import warnings
import numpy as np
import torch
import asdl
from asdl.matrices import (
    FISHER_EXACT, FISHER_MC, FISHER_EMP, SHAPE_KRON, SHAPE_DIAG, SHAPE_FULL
)
from asdl.grad_maker import LOSS_MSE, LOSS_CROSS_ENTROPY
from asdl.fisher import FisherConfig, get_fisher_maker
# from asdl.fisher import calculate_fisher
from asdl.hessian import HessianMaker, HessianConfig
from asdl.gradient import batch_gradient

# from asdl import FISHER_EXACT, FISHER_MC #, COV
# from asdl import SHAPE_KRON, SHAPE_DIAG, SHAPE_FULL
# from asdl import fisher_for_cross_entropy
# from asdl.hessian import hessian_eigenvalues, hessian_for_loss
# from asdl.gradient import batch_gradient

import torch
import torch.distributed as dist
from torch.nn.utils import parameters_to_vector, vector_to_parameters
from asdl.core import extend
from asdl.operations import OP_BATCH_GRADS
import asdl
from tqdm import tqdm


def batch_gradient(model, closure, input_shape,return_outputs=False):

    cnt = 0
    flatten = lambda x: [item for y in x for item in y]

    ignore_modules = model.get_ignore_modules(model)
    if (batch_size := getattr(model, 'batched_modules',0)) > 0:
        all_modules = [m for m in model.modules() if getattr(m, 'partial', False)]
        batches = [all_modules[(i * batch_size): (i +1)*batch_size]
                   for i in range(int(len(all_modules)/batch_size))]

        num_collected_batches = sum([len(ba) for ba in batches])
        length_difference = len(all_modules) - num_collected_batches
        if length_difference != 0:
            batches.append(all_modules[-length_difference:])

    else:
        batches = [""]
    grads = []
    pbar = tqdm(batches, desc='Getting batched x2 gradients')

    for idx, batch in enumerate(pbar):
        extra_ignore_modules = flatten([ba for i, ba in enumerate(batches) if i != idx])
        with extend(model, OP_BATCH_GRADS, ignore_modules=ignore_modules + extra_ignore_modules) as cxt:
            outputs = closure()
            N = input_shape[0]
            L = input_shape[-1]
            for module in model.modules():
                cnt += 1
                g = cxt.batch_grads(module, flatten=True)
                if g is not None:
                    if len(input_shape) == 2:
                        if g.shape[0] > N:
                            grads.append(g.reshape(*input_shape,-1).sum(-2))
                        else:
                            grads.append(g)
                    else:
                        if g.shape[0] > N*L:
                            grads.append(g.reshape(*input_shape,-1).sum(-2).sum(-2))
                        elif g.shape[0] > N:
                            grads.append(g.reshape(*input_shape[:-1],-1).sum(-2))
                        else:
                            grads.append(g)

                    if hasattr(module, 'subnetwork_indices'):
                        grads[-1] = grads[-1][module.subnetwork_indices].unsqueeze(0)

    pbar.close()
    grads = torch.cat(grads, dim=-1)  # (n, p)
    if return_outputs:
        return grads, outputs
    else:
        return grads


asdl.batch_gradient = batch_gradient


from laplace_partial.curvature import CurvatureInterface, GGNInterface, EFInterface
from laplace_partial.utils import Kron, _is_batchnorm

EPS = 1e-6


class AsdlInterface(CurvatureInterface):
    """Interface for asdfghjkl backend.
    """
    def __init__(self, model, likelihood, last_layer=False, subnetwork_indices=None,
                 kfac_conv='kfac-expand'):
        super().__init__(model, likelihood, last_layer, subnetwork_indices)
        self.kfac_conv = kfac_conv

    @property
    def loss_type(self):
        return LOSS_MSE if self.likelihood == 'regression' else LOSS_CROSS_ENTROPY

    def jacobians(self, batch):
        """Compute Jacobians \\(\\nabla_\\theta f(x;\\theta)\\) at current parameter \\(\\theta\\)
        using asdfghjkl's gradient per output dimension.

        Parameters
        ----------
        x : torch.Tensor
            input data `(batch, input_shape)` on compatible device with model.

        Returns
        -------
        Js : torch.Tensor
            Jacobians `(batch, parameters, outputs)`
        f : torch.Tensor
            output function `(batch, outputs)`
        """
        x = batch['input_ids']
        input_shape =  x.shape

        Js = list()
        for i in range(self.model.output_size):
            def closure():
                self.model.zero_grad()
                f = self.model(**batch)
                loss = f[:, i].sum()
                loss.backward()
                return f

            Ji, f = batch_gradient(self.model, closure, x.shape, return_outputs=True)
            if self.subnetwork_indices is not None:
                Ji = Ji[:, self.subnetwork_indices]
            # if Ji.shape[0] > N:
                # p = Ji.shape[-1]
                # Ji = Ji.reshape(N,L,p).sum(1)
            Js.append(Ji)
        Js = torch.stack(Js, dim=1)
        return Js, f
    def resize_B(self, B, indices):
        B = B[indices]
        B = B[:, indices]
        return B

    def gradients(self, x, y):
        """Compute gradients \\(\\nabla_\\theta \\ell(f(x;\\theta, y)\\) at current parameter
        \\(\\theta\\) using asdfghjkl's backend.

        Parameters
        ----------
        x : torch.Tensor
            input data `(batch, input_shape)` on compatible device with model.
        y : torch.Tensor

        Returns
        -------
        loss : torch.Tensor
        Gs : torch.Tensor
            gradients `(batch, parameters)`
        """
        print('=======using gradients() function=======')
        def closure():
            self.model.zero_grad()
            loss = self.lossfunc(self.model(x), y)
            loss.backward()
            return loss

        Gs, loss = batch_gradient(self.model, closure, return_outputs=True)
        if self.subnetwork_indices is not None:
            Gs = Gs[:, self.subnetwork_indices]
        return Gs, loss

    @property
    def _ggn_type(self):
        raise NotImplementedError


    def _get_kron_factors(self, M):
        kfacs = list()
        for module in self.model.modules():
            if _is_batchnorm(module):
                warnings.warn('BatchNorm unsupported for Kron, ignore.')
                continue

            if isinstance(module, torch.nn.Embedding):
                continue

            if not getattr(module, 'partial', True):
                continue

            stats = getattr(module, 'fisher', None)
            if stats is None:
                continue

            if hasattr(module, 'bias') and module.bias is not None:
                # split up bias and weights
                if getattr(module, 'subsample_fisher_A_B', False):
                    (ind_input, ind_output), ind_bias = module.param_indices
                    kfacs.append([self.resize_B(stats.kron.B, ind_output),
                                  self.resize_B(stats.kron.A, ind_input)])
                    kfacs.append([self.resize_B(stats.kron.B, ind_bias)])
                else:
                    kfacs.append([stats.kron.B, stats.kron.A])
                    kfacs.append([stats.kron.B])

            elif hasattr(module, 'weight'):
                p, q = np.prod(stats.kron.B.shape), np.prod(stats.kron.A.shape)
                if p == q == 1:
                    kfacs.append([stats.kron.B * stats.kron.A])
                else:
                    kfacs.append([stats.kron.B, stats.kron.A])
            else:
                raise ValueError(f'Whats happening with {module}?')
        return Kron(kfacs)
    @staticmethod
    def _rescale_kron_factors(kron, N):
        for F in kron.kfacs:
            if len(F) == 2:
                F[1] *= 1/N
        return kron

    def diag(self, batch, **kwargs):
        y = batch['labels']
        if self.last_layer:
            _, X = self.model.forward_with_features(**batch)

            cfg = FisherConfig(fisher_type=self._ggn_type, loss_type=self.loss_type,
                            fisher_shapes=[SHAPE_DIAG], data_size=1)
            fisher_maker = get_fisher_maker(self.model, cfg)
            if 'emp' in self._ggn_type:
                dummy = fisher_maker.setup_model_call(self._model, X)
                fisher_maker.setup_loss_call(self.lossfunc, dummy, y)
            else:
                fisher_maker.setup_model_call(self._model, X)
        else:
            cfg = FisherConfig(fisher_type=self._ggn_type, loss_type=self.loss_type,
                            fisher_shapes=[SHAPE_DIAG], data_size=1)
            fisher_maker = get_fisher_maker(self.model, cfg)
            if 'emp' in self._ggn_type:
                dummy = fisher_maker.setup_model_call(self._model, **batch)
                fisher_maker.setup_loss_call(self.lossfunc, dummy, y)
            else:
                fisher_maker.setup_model_call(self._model, **batch)

        f, _ = fisher_maker.forward_and_backward()
        print('f shape', f.shape)
        print('y shape', y.shape)
        loss = self.lossfunc(f.detach(), y)
        vec = list()
        for module in self.model.modules():
            stats = getattr(module, 'fisher', None)
            if stats is None:
                # print('stats is None', module)
                continue
            vec.extend(stats.to_vector())
        diag_ggn = torch.cat(vec)
        if self.subnetwork_indices is not None:
            diag_ggn = diag_ggn[self.subnetwork_indices]
        if type(self) is AsdlEF and self.likelihood == 'regression':
            curv_factor = 0.5  # correct scaling for diag ef
        else:
            curv_factor = 1.0   # ASDL uses proper 1/2 * MSELoss
        return self.factor * loss, curv_factor * diag_ggn, f.detach()
 
    def kron(self, batch, N, **kwargs):
        y = batch['labels']
        if self.last_layer:
            _, X = self.model.forward_with_features(**batch)

            cfg = FisherConfig(fisher_type=self._ggn_type, loss_type=self.loss_type,
                           fisher_shapes=[SHAPE_KRON], data_size=1)
            # fisher_maker = get_fisher_maker(self.model, cfg, self.kfac_conv)
            fisher_maker = get_fisher_maker(self.model, cfg)
            if 'emp' in self._ggn_type:
                dummy = fisher_maker.setup_model_call(self._model, X)
                fisher_maker.setup_loss_call(self.lossfunc, dummy, y)
            else:
                fisher_maker.setup_model_call(self._model, X)

        else:
            cfg = FisherConfig(fisher_type=self._ggn_type, loss_type=self.loss_type,
                            fisher_shapes=[SHAPE_KRON], data_size=1)
            # fisher_maker = get_fisher_maker(self.model, cfg, self.kfac_conv)
            fisher_maker = get_fisher_maker(self.model, cfg)
            if 'emp' in self._ggn_type:
                dummy = fisher_maker.setup_model_call(self._model, **batch)
                fisher_maker.setup_loss_call(self.lossfunc, dummy, y)
            else:
                fisher_maker.setup_model_call(self._model, **batch)

        f, _ = fisher_maker.forward_and_backward()
        loss = self.lossfunc(f.detach(), y)
        M = len(y)
        kron = self._get_kron_factors(M)
        kron = self._rescale_kron_factors(kron, N)
        if type(self) is AsdlEF and self.likelihood == 'regression':
            curv_factor = 0.5  # correct scaling for diag ef
        else:
            curv_factor = 1.0   # ASDL uses proper 1/2 * MSELoss
        return self.factor * loss, curv_factor * kron, f.detach()


class AsdlHessian(AsdlInterface):

    def __init__(self, model, likelihood, last_layer=False, low_rank=10):
        super().__init__(model, likelihood, last_layer)
        self.low_rank = low_rank

    @property
    def _ggn_type(self):
        raise NotImplementedError()

    def full(self, x, y, **kwargs):
        if self.last_layer:
            _, x = self.model.forward_with_features(x)
        cfg = HessianConfig(hessian_shapes=[SHAPE_FULL])
        hess_maker = HessianMaker(self.model, cfg)
        dummy = hess_maker.setup_model_call(self._model, x)
        hess_maker.setup_loss_call(self.lossfunc, dummy, y)
        hess_maker.forward_and_backward()
        H = self._model.hessian.data
        loss = self.lossfunc(self.model(x), y).detach()
        return self.factor * loss, self.factor * H

    def eig_lowrank(self, data_loader):
        # TODO: need to implement manually...
        # compute truncated eigendecomposition of the Hessian, only keep eigvals > EPS
        if self.last_layer:
            _, x = self.model.forward_with_features(x)
        cfg = HessianConfig(hessian_shapes=[SHAPE_FULL])
        hess_maker = HessianMaker(self.model, cfg)
        dummy = hess_maker.setup_model_call(self._model, x)
        hess_maker.setup_loss_call(self.lossfunc, dummy, y)
        # iteratively go through data loader and average eigendecomposition
        # previously:
        eigvals, eigvecs = hessian_eig(self.model, self.lossfunc, data_loader=data_loader,
                                       top_n=self.low_rank, max_iters=self.low_rank*10)
        eigvals = torch.from_numpy(np.array(eigvals))
        mask = (eigvals > EPS)
        eigvecs = torch.stack([vec.get_flatten_vector() for vec in eigvecs], dim=1)[:, mask]
        device = eigvecs.device
        eigvals = eigvals[mask].to(eigvecs.dtype).to(device)
        loss = sum([self.lossfunc(self.model(x.to(device)).detach(), y.to(device)) for x, y in data_loader])
        return eigvecs, self.factor * eigvals, self.factor * loss


class AsdlGGN(AsdlInterface, GGNInterface):
    """Implementation of the `GGNInterface` using asdfghjkl.
    """
    def __init__(self, model, likelihood, last_layer=False, subnetwork_indices=None, stochastic=False,
                 kfac_conv='kfac-expand'):
        super().__init__(model, likelihood, last_layer, subnetwork_indices, kfac_conv=kfac_conv)
        self.stochastic = stochastic

    @property
    def _ggn_type(self):
        return FISHER_MC if self.stochastic else FISHER_EXACT


class AsdlEF(AsdlInterface, EFInterface):
    """Implementation of the `EFInterface` using asdfghjkl.
    """
    def __init__(self, model, likelihood, last_layer=False, kfac_conv='kfac-expand'):
        super().__init__(model, likelihood, last_layer, kfac_conv=kfac_conv)

    @property
    def _ggn_type(self):
        return FISHER_EMP
