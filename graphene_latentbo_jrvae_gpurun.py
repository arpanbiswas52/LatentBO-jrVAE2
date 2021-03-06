# -*- coding: utf-8 -*-
"""Graphene_latentBO-jrVAE_gpurun.ipynb
Automatically generated by Colaboratory.
Original file is located at
    https://colab.research.google.com/drive/1YBhJSHjSxa2orCeWEEket65wWQUy_tVh
# Learning (jointly) discrete and continuous representations of the arbitrary rotated image data
---
Here we introduce a joint (rotationally-invariant) VAE that can perform unsupervised classification and disentangle relevant continuous factors of variation at the same time.
---
jrVAE model prepared by Maxim Ziatdinov
KL factor optimization framework (using contrained BO) in 2D latent space (dimension recuction using VAE) prepared by Arpan Biswas
Workflow implemented on Graphene examples where we want to separate different classes of defects - no prior knowledge of # of defect classes
E-mail: ziatdinovmax@gmail.com
E-mail: arpanbiswas52@gmail.com
"""

# @title Installation
# !pip install -q pyroved kornia
# !pip install botorch #version 0.5.1
# !pip install gpytorch #version 1.6.0
# !pip install pyroved
# !pip install atomai==0.5.2 > /dev/null
# !pip install smt

# @title Imports
from typing import Tuple

import pyroved as pv
import torch
import kornia as K
import kornia.metrics as metrics
from torchvision import datasets
import matplotlib.pyplot as plt
import numpy as np
import random

# Import GP and BoTorch functions
import gpytorch as gpt
from botorch.models import SingleTaskGP, ModelListGP
# from botorch.models import gpytorch
from botorch.fit import fit_gpytorch_model
from botorch.models.gpytorch import GPyTorchModel
from botorch.utils import standardize
from gpytorch.distributions import MultivariateNormal
from gpytorch.kernels import ScaleKernel, RBFKernel, MaternKernel
from gpytorch.likelihoods import GaussianLikelihood
from gpytorch.means import ConstantMean, LinearMean
from gpytorch.mlls import ExactMarginalLogLikelihood
from botorch.acquisition import UpperConfidenceBound
from botorch.optim import optimize_acqf
from botorch.acquisition import qExpectedImprovement
from botorch.acquisition import ExpectedImprovement
from botorch.sampling import IIDNormalSampler
from botorch.sampling import SobolQMCNormalSampler
from gpytorch.likelihoods.likelihood import Likelihood
from gpytorch.constraints import GreaterThan

from botorch.generation import get_best_candidates, gen_candidates_torch
from botorch.optim import gen_batch_initial_conditions

from gpytorch.models import ExactGP
from mpl_toolkits.axes_grid1 import make_axes_locatable
# from smt.sampling_methods import LHS
from torch.optim import SGD
from torch.optim import Adam
from scipy.stats import norm
from scipy.interpolate import interp1d

# import atomai as aoi

from smt.sampling_methods import LHS


# @title Helper functions

def func_periodic(x, params):
    y = params["A"] * np.exp(params["alpha"] * x) * np.cos(params["omega"] * x) + params["B"] * x
    return y


def generate_1Dspectra_Segment(degree, nsamples) -> torch.Tensor:
    dataset = []
    points = []
    slopes = []

    for i in range(nsamples):

        x = np.linspace(-1, 1, 120)
        segment_x = np.array([-1] + list(sorted(np.random.uniform(-1, 1, degree))) + [1])
        segment_y = np.random.uniform(0, 1, degree + 2)
        points.append(torch.from_numpy(segment_x))

        slope = np.zeros(degree + 1)
        for j in range(len(slope)):
            slope[j] = (segment_y[j + 1] - segment_y[j]) / (segment_x[j + 1] - segment_x[j])
        slope = torch.from_numpy(slope)

        f2 = interp1d(segment_x, segment_y, kind='linear')
        f2 = f2(x)
        f2 = torch.from_numpy(f2).type(torch.float)
        # noise = torch.randint(0, 50, (1,)) / 1e3
        noise = 0  # no noise

        data_t = f2 + noise * torch.randn(size=(len(x),))
        dataset.append(data_t)
        slopes.append(slope)

    dataset = torch.cat(dataset).reshape(nsamples, 120)
    points = torch.cat(points).reshape(nsamples, degree + 2)
    slopes = torch.cat(slopes).reshape(nsamples, degree + 1)

    # dataset = (dataset - dataset.min()) / (dataset.max() - dataset.min())
    return dataset, points, slopes


"""#Functions defined for problem and objectives
#Task
- Build objective function with maximizing mean loss or the Structural dissimilarity (DSSIM) among the manifolds representing each discrete class
- Build BO framework after reducing the high dimension trajectory hyper-parameter. Here from sensitivity analysis, we found the time-dependent trajectory, which varies the performance of VAE, depends on 4 independent components- time to start cool down, starting value, rate of cooldown and final value. We can reduce a large dimensional problem (>100D) into 4D problem.
"""


# @title SSIM loss function between images
def ssim_loss(
        img1: torch.Tensor,
        img2: torch.Tensor,
        window_size: int,
        max_val: float = 1.0,
        eps: float = 1e-12,
        reduction: str = 'mean',
) -> torch.Tensor:
    r"""Function that computes a loss based on the SSIM measurement.
    The loss, or the Structural dissimilarity (DSSIM) is described as:
    .. math::
      \text{loss}(x, y) = \frac{1 - \text{SSIM}(x, y)}{2}
    See :meth:`~kornia.losses.ssim` for details about SSIM.
    Args:
        img1: the first input image with shape :math:`(B, C, H, W)`.
        img2: the second input image with shape :math:`(B, C, H, W)`.
        window_size: the size of the gaussian kernel to smooth the images.
        max_val: the dynamic range of the images.
        eps: Small value for numerically stability when dividing.
        reduction : Specifies the reduction to apply to the
         output: ``'none'`` | ``'mean'`` | ``'sum'``. ``'none'``: no reduction will be applied,
         ``'mean'``: the sum of the output will be divided by the number of elements
         in the output, ``'sum'``: the output will be summed.
    Returns:
        The loss based on the ssim index.
    Examples:
        >>> input1 = torch.rand(1, 4, 5, 5)
        >>> input2 = torch.rand(1, 4, 5, 5)
        >>> loss = ssim_loss(input1, input2, 5)
    """
    # compute the ssim map
    ssim_map: torch.Tensor = metrics.ssim(img1, img2, window_size, max_val, eps)

    # compute and reduce the loss
    loss = torch.clamp((1.0 - ssim_map) / 2, min=0, max=1)

    if reduction == "mean":
        loss = torch.mean(loss)
    elif reduction == "sum":
        loss = torch.sum(loss)
    elif reduction == "none":
        pass
    return loss


# @title SSIM Loss objective function- Combined objective to minimize the ssim among the manifolds representing discrete classes, thus maximize the loss; and to maximize the ssim within the manifolds representing each discrete classes, thus minimize the loss
def loss_obj(X, data, batch_size, B, H, W, discrete_dim):
    # xx=float(X)
    pen = 10 ** 0
    data_dim = (H, W)
    M = torch.empty(B * B, H, W, discrete_dim)
    loss1 = 0
    loss2 = 0
    train_loader_X = pv.utils.init_dataloader(data, batch_size=batch_size)
    jvae_X = pv.models.jiVAE(data_dim, latent_dim=2, discrete_dim=discrete_dim, invariances=['r'], seed=42)

    trainer_X = pv.trainers.SVItrainer(jvae_X, lr=1e-3, enumerate_parallel=True)

    kl_scale = torch.from_numpy(X)
    # print(kl_scale.shape)
    for i in range(120):
        sc = kl_scale[i] if i < len(kl_scale) else kl_scale[-1]
        trainer_X.step(train_loader_X, scale_factor=[sc, sc])
        # loss[i] = trainer_X.loss_history["training_loss"][-1]

    for i in range(discrete_dim):
        M[:, :, :, i] = jvae_X.manifold2d(d=B, disc_idx=i, plot=False)

    M = torch.reshape(M, (M.shape[0], 1, M.shape[1], M.shape[2], M.shape[3]))
    k1 = 0
    # Objective 1 is to minimize the ssim among the manifolds representing discrete classes, thus maximize the loss
    for i in range(discrete_dim):
        for j in range(discrete_dim):
            if (j > i):
                M1 = M[:, :, :, :, i]
                M2 = M[:, :, :, :, j]
                # print(M1.shape, M2.shape)
                # Compute SSIM/loss among each manifolds
                loss1 = loss1 + ssim_loss(M1, M2, 5)
                k1 = k1 + 1
    # obj1 = (loss1/k1)*pen
    obj1 = (loss1) * pen

    # Objective 2 is to maximize the ssim within the manifolds representing each discrete classes, thus minimize the loss

    np.random.seed(0)
    n_image = 1000
    idxy = np.random.randint(0, B * B, (n_image, 2))
    for i in range(discrete_dim):
        k2 = 0
        l2 = 0
        for j in range(n_image):
            if (idxy[i, 0] is not idxy[i, 1]):
                loc1 = idxy[i, 0]
                loc2 = idxy[i, 1]
                m1 = M[loc1, :, :, :, i]
                m2 = M[loc2, :, :, :, i]
                m1 = torch.reshape(m1, (1, m1.shape[0], m1.shape[1], m1.shape[2]))
                m2 = torch.reshape(m2, (1, m2.shape[0], m2.shape[1], m2.shape[2]))
                # print(m1.shape, m2.shape)
                # Compute SSIM/loss within each manifolds
                l2 = l2 + ssim_loss(m1, m2, 5)
                k2 = k2 + 1

        loss2 = loss2 + (l2 / k2)

    # obj2 = (loss2/discrete_dim)*pen
    obj2 = (loss2) * pen

    obj = obj1 - obj2  # obj2 converted into maximization problem

    return obj


# @title SSIM Loss objective function - Objective is to minimize the ssim among the manifolds representing discrete classes, thus maximize the loss
def loss_obj2(X, data, batch_size, B, H, W, discrete_dim):
    # xx=float(X)
    data_dim = (H, W)
    M = torch.empty(B * B, H, W, discrete_dim)
    loss = 0
    train_loader_X = pv.utils.init_dataloader(data, batch_size=batch_size)
    jvae_X = pv.models.jiVAE(data_dim, latent_dim=2, discrete_dim=discrete_dim, invariances=['r'], seed=42)

    trainer_X = pv.trainers.SVItrainer(jvae_X, lr=1e-3, enumerate_parallel=True)

    kl_scale = torch.from_numpy(X)
    # print(kl_scale.shape)
    for i in range(120):
        sc = kl_scale[i] if i < len(kl_scale) else kl_scale[-1]
        trainer_X.step(train_loader_X, scale_factor=[sc, sc])
        # loss[i] = trainer_X.loss_history["training_loss"][-1]

    for i in range(discrete_dim):
        M[:, :, :, i] = jvae_X.manifold2d(d=B, disc_idx=i, plot=False)

    M = torch.reshape(M, (M.shape[0], 1, M.shape[1], M.shape[2], M.shape[3]))
    k = 0
    for i in range(discrete_dim):
        for j in range(discrete_dim):
            if (j > i):
                M1 = M[:, :, :, :, i]
                M2 = M[:, :, :, :, j]
                # Compute SSIM/loss among each manifolds
                loss = loss + ssim_loss(M1, M2, 5)
                k = k + 1
    pen = 10 ** 0
    # Objective is to minimize the ssim among the manifolds representing discrete classes, thus maximize the loss
    # obj = (loss/k)*pen
    obj = (loss) * pen

    return obj


# @title Eliminate infeasible latent space from data
def getfeasible(X, fix_model):
    X_f = np.zeros((X.shape[1] ** X.shape[0], X.shape[0]))
    z = torch.empty((1, 2))
    k = 0
    for t1 in range(0, X.shape[1]):
        for t2 in range(0, X.shape[1]):
            z[0, 0] = X[0, t1]
            z[0, 1] = X[1, t2]
            decoded_traj = fix_model.decode(z).numpy()
            if (np.min(decoded_traj) > 0):
                # decoded_traj_feas[t1, t2] = 1 #1 denotes feasible
                X_f[k, 0] = X[0, t1]
                X_f[k, 1] = X[1, t2]
                k = k + 1
            else:
                # decoded_traj_feas[t1, t2] = 0 #0 denotes infeasible
                X_f[k, 0] = np.inf
                X_f[k, 1] = np.inf
                k = k + 1

    X_feas = np.transpose(np.vstack((X_f[~np.isinf(X_f[:, 0]), 0], X_f[~np.isinf(X_f[:, 1]), 1])))
    X_feas = torch.from_numpy(X_feas)
    # print(X_f.shape)
    # print(X_feas.shape)
    return X_feas


"""#Functions defined for BO architecture
Below section defines the list of functions (user calls these functions during analysis):
1. Gaussian Process
2. Optimizize Hyperparameter of Gaussian Process (using Adam optimizer)
3. Posterior means and variance computation
4. Acquistion functions for BO- Expected Improvmement
"""


class SimpleCustomGP(ExactGP, GPyTorchModel):
    _num_outputs = 1  # to inform GPyTorchModel API

    def __init__(self, train_X, train_Y):
        # squeeze output dim before passing train_Y to ExactGP
        super().__init__(train_X, train_Y.squeeze(-1), GaussianLikelihood())
        self.mean_module = ConstantMean()
        # self.mean_module = LinearMean(train_X.shape[-1])
        self.covar_module = ScaleKernel(
            # base_kernel=MaternKernel(nu=2.5, ard_num_dims=train_X.shape[-1]),
            base_kernel=RBFKernel(ard_num_dims=train_X.shape[-1]),
        )
        self.to(train_X)  # make sure we're on the right device/dtype

    def forward(self, x):
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return MultivariateNormal(mean_x, covar_x)


# Optimize Hyperparameters of GP#
def optimize_hyperparam_trainGP(train_X, train_Y):
    # Gp model fit

    gp_surro = SimpleCustomGP(train_X, train_Y)
    gp_surro = gp_surro.double()
    gp_surro.likelihood.noise_covar.register_constraint("raw_noise", GreaterThan(1e-1))
    mll1 = ExactMarginalLogLikelihood(gp_surro.likelihood, gp_surro)
    # fit_gpytorch_model(mll)
    mll1 = mll1.to(train_X)
    gp_surro.train()
    gp_surro.likelihood.train()
    ## Here we use Adam optimizer with learning rate =0.1, user can change here with different algorithm and/or learning rate for each GP
    optimizer1 = Adam([{'params': gp_surro.parameters()}], lr=0.0001)
    # optimizer1 = SGD([{'params': gp_surro.parameters()}], lr=0.0001)

    NUM_EPOCHS = 150

    for epoch in range(NUM_EPOCHS):
        # clear gradients
        optimizer1.zero_grad()
        # forward pass through the model to obtain the output MultivariateNormal
        output1 = gp_surro(train_X)
        # Compute negative marginal log likelihood
        loss1 = - mll1(output1, gp_surro.train_targets)
        # back prop gradients
        loss1.backward(retain_graph=True)
        # print last iterations
        if (epoch + 1) > NUM_EPOCHS:  # Stopping the print for now
            print("GP Model trained:")
            print("Iteration:" + str(epoch + 1))
            print("Loss:" + str(loss1.item()))
            # print("Length Scale:" +str(gp_PZO.covar_module.base_kernel.lengthscale.item()))
            print("noise:" + str(gp_surro.likelihood.noise.item()))

        optimizer1.step()

    gp_surro.eval()
    gp_surro.likelihood.eval()
    return gp_surro


# GP posterior predictions#
def cal_posterior(gp_surro, test_X):
    y_pred_means = torch.empty(len(test_X), 1)
    y_pred_vars = torch.empty(len(test_X), 1)
    t_X = torch.empty(1, test_X.shape[1])
    for t in range(0, len(test_X)):
        with torch.no_grad(), gpt.settings.max_lanczos_quadrature_iterations(32), \
                gpt.settings.fast_computations(covar_root_decomposition=False, log_prob=False,
                                               solves=True), \
                gpt.settings.max_cg_iterations(100), \
                gpt.settings.max_preconditioner_size(80), \
                gpt.settings.num_trace_samples(128):
            t_X[:, 0] = test_X[t, 0]
            t_X[:, 1] = test_X[t, 1]
            # t_X = test_X.double()
            y_pred_surro = gp_surro.posterior(t_X)
            y_pred_means[t, 0] = y_pred_surro.mean
            y_pred_vars[t, 0] = y_pred_surro.variance

    return y_pred_means, y_pred_vars


# EI acquistion function#
def acqmanEI(y_pred_means, y_pred_vars, train_Y):
    y_pred_means = y_pred_means.detach().numpy()
    y_pred_vars = y_pred_vars.detach().numpy()
    fmax = train_Y.max()
    fmax = fmax.detach().numpy()
    best_value = fmax
    EI_val = np.zeros(len(y_pred_vars))
    Z = np.zeros(len(y_pred_vars))
    eta = 0.001

    for i in range(0, len(y_pred_vars)):
        if (y_pred_vars[i] <= 0):
            EI_val[i] = 0
        else:
            Z[i] = (y_pred_means[i] - best_value - eta) / y_pred_vars[i]
            EI_val[i] = (y_pred_means[i] - best_value - eta) * norm.cdf(Z[i]) + y_pred_vars[i] * norm.pdf(Z[i])

    acq_val = np.max(EI_val)
    acq_cand = [k for k, j in enumerate(EI_val) if j == acq_val]
    return acq_cand, acq_val, EI_val


"""#Other list of functions-
1. Evaluate initial data and normalize all data
2. Evaluate functions for new data and augment data
"""


# Normalize all data. It is very important to fit GP model with normalized data to avoid issues such as
# - decrease of GP performance due to largely spaced real-valued data X.
def normalize_get_initialdata_KL(X, fix_params, data, fix_model, num_rows, num, m):
    # Eliminate infeasible region in the latent space
    X_feas = getfeasible(X, fix_model)

    X_feas_norm = torch.empty((X_feas.shape[0], X_feas.shape[1]))
    # train_X = torch.empty((len(X), num))
    # train_X_norm = torch.empty((len(X), num))
    train_Y = torch.empty((num, 1))

    # Normalize X
    for i in range(0, X_feas.shape[1]):
        X_feas_norm[:, i] = (X_feas[:, i] - torch.min(X_feas[:, i])) / (
                    torch.max(X_feas[:, i]) - torch.min(X_feas[:, i]))

    # Select starting samples randomly as training data
    np.random.seed(0)
    xlimits = np.array([[0, len(X_feas)]])
    sampling = LHS(xlimits=xlimits)

    idx = sampling(num)
    idx = np.reshape(idx, (idx.shape[0] * idx.shape[1]))
    idx = np.round(idx)
    # idx = np.random.randint(0, len(X_feas), num)
    train_X = X_feas[idx]
    train_X_norm = X_feas_norm[idx]

    # Saving/Updating data at each iterations
    np.save("train_X.npy", train_X)
    np.save("train_X_norm.npy", train_X_norm)

    # Evaluate initial training data
    z = torch.empty((1, 2))
    for i in range(0, num):
        z[0, 0] = train_X[i, 0]
        z[0, 1] = train_X[i, 1]
        decoded_traj = fix_model.decode(z).numpy()
        decoded_traj1 = np.reshape(decoded_traj, (decoded_traj.shape[0] * decoded_traj.shape[1]))
        print("Function eval #" + str(m + 1))
        batch_size, B, H, W, discrete_dim  = fix_params[0], fix_params[1], fix_params[2], fix_params[3], fix_params[4]

        train_Y[i, 0] = loss_obj(decoded_traj1, data, batch_size, B, H, W, discrete_dim)
        m = m + 1
        # Saving/Updating data at each iterations
        np.save("train_Y.npy", train_Y)
        np.save("m.npy", m)

    return X_feas, X_feas_norm, train_X, train_X_norm, train_Y, m


################################Augment data - Existing training data with new evaluated data################################
def augment_newdata_KL(acq_X, acq_X_norm, train_X, train_X_norm, train_Y, fix_params, data, fix_model, m):
    nextX = acq_X
    nextX_norm = acq_X_norm
    # train_X_norm = torch.cat((train_X_norm, nextX_norm), 0)
    # train_X_norm = train_X_norm.double()
    train_X_norm = torch.vstack((train_X_norm, nextX_norm))
    train_X = torch.vstack((train_X, nextX))
    next_feval = torch.empty(1, 1)
    z = torch.empty((1, 2))
    z[0, 0] = train_X[-1, 0]
    z[0, 1] = train_X[-1, 1]
    decoded_traj = fix_model.decode(z).numpy()
    decoded_traj1 = np.reshape(decoded_traj, (decoded_traj.shape[0] * decoded_traj.shape[1]))
    print("Function eval #" + str(m + 1))
    batch_size, B, H, W, discrete_dim = fix_params[0], fix_params[1], fix_params[2], fix_params[3], fix_params[4]

    next_feval[0, 0] = loss_obj(decoded_traj1, data, batch_size, B, H, W, discrete_dim)

    train_Y = torch.vstack((train_Y, next_feval))

    # train_Y = torch.cat((train_Y, next_feval), 0)
    m = m + 1
    return train_X, train_X_norm, train_Y, m


# @title Functions to plot KL trajectories at specific BO iterations
def plot_iteration_results(train_X, train_Y, test_X, y_pred_means, y_pred_vars, fix_model, i):
    pen = 10 ** 0
    # Best solution among the evaluated data

    loss = torch.max(train_Y)
    ind = torch.argmax(train_Y)
    z_opt = torch.empty((1, 2))
    # X_opt = train_X[ind, :]
    z_opt[0, 0] = train_X[ind, 0]
    z_opt[0, 1] = train_X[ind, 1]
    decoded_traj = fix_model.decode(z_opt).numpy()
    decoded_traj1 = np.reshape(decoded_traj, (decoded_traj.shape[0] * decoded_traj.shape[1]))
    kl_scale_eval = torch.from_numpy(decoded_traj1)
    n = len(kl_scale_eval)
    plt.figure()
    plt.plot(np.linspace(1, n, n), kl_scale_eval.detach().numpy(), 'ro-', markersize=2, linewidth=1)
    plt.xlabel("steps")
    plt.ylabel("kl")
    plt.title("Best evaluated solution at iteration: " + str(i) + ", Loss:" + str(loss / pen))
    # print("Loss: " + str(loss))
    plt.savefig("Eval sol at iter" + str(i) + ".png")
    plt.show()

    # Best estimated solution from GP model considering the non-evaluated solution
    # robust_Y = y_pred_means - (y_pred_vars/2) # Increasing preference, (r = mu - (sigma2/2*rho)), risk attitute, rho = 1
    robust_Y = y_pred_means
    loss = torch.max(robust_Y)
    ind = torch.argmax(robust_Y)
    z_opt_robust = torch.empty((1, 2))
    # X_opt = train_X[ind, :]
    z_opt_robust[0, 0] = test_X[ind, 0]
    z_opt_robust[0, 1] = test_X[ind, 1]
    decoded_traj = fix_model.decode(z_opt_robust).numpy()
    decoded_traj1 = np.reshape(decoded_traj, (decoded_traj.shape[0] * decoded_traj.shape[1]))
    kl_scale_est = torch.from_numpy(decoded_traj1)
    n = len(kl_scale_est)
    plt.figure()
    plt.plot(np.linspace(1, n, n), kl_scale_est.detach().numpy(), 'ro-', markersize=2, linewidth=1)
    plt.xlabel("steps")
    plt.ylabel("kl")
    plt.title("Best estimated solution at iteration: " + str(i) + ", Loss:" + str(y_pred_means[ind] / pen))
    # print("Loss: " + str(loss))
    plt.savefig("Est sol at iter" + str(i) + ".png")
    plt.show()

    # Objective map over 2D latent space
    plt.figure()
    a = plt.scatter(test_X[:, 0], test_X[:, 1], c=y_pred_means / pen, cmap='viridis', linewidth=0.2)
    plt.scatter(train_X[:, 0], train_X[:, 1], marker='o', c='g')
    plt.scatter(z_opt[0, 0], z_opt[0, 1], marker='x', c='r')
    plt.scatter(z_opt_robust[0, 0], z_opt_robust[0, 1], marker='o', c='r')
    plt.xlabel('z1')
    plt.ylabel('z2')
    plt.title('Objective (SSIM loss) map over feasible 2D latent space')
    plt.colorbar(a)
    plt.savefig("Obj map at iter" + str(i) + ".png")
    plt.show()

    # Objective map over 2D latent space
    plt.figure()
    plt.scatter(test_X[:, 0], test_X[:, 1], c=y_pred_vars / (pen ** 2), cmap='viridis', linewidth=0.2)
    plt.xlabel('z1')
    plt.ylabel('z2')
    plt.title('Objective var map over feasible 2D latent space')
    plt.colorbar()
    plt.savefig("Obj var map at iter" + str(i) + ".png")
    plt.show()

    return kl_scale_eval, kl_scale_est


# @title BO framework- Integrating the above functions
def latentBO_KL(X, fix_params, data, fix_model, num_rows, num_start, N):
    num = num_start
    m = 0
    # Initialization: evaluate few initial data normalize data
    test_X, test_X_norm, train_X, train_X_norm, train_Y, m = \
        normalize_get_initialdata_KL(X, fix_params, data, fix_model, num_rows, num, m)

    print("Initial evaluation complete. Start BO")
    ## Gp model fit
    # Calling function to fit and optimizize Hyperparameter of Gaussian Process (using Adam optimizer)
    # Input args- Torch arrays of normalized training data, parameter X and objective eval Y
    # Output args- Gaussian process model lists
    gp_surro = optimize_hyperparam_trainGP(train_X_norm, train_Y)

    for i in range(1, N + 1):
        # Calculate posterior for analysis for intermidiate iterations
        y_pred_means, y_pred_vars = cal_posterior(gp_surro, test_X_norm)
        if ((i - 1) % 5 == 0):
            # Plotting functions to check the current state exploration and Pareto fronts
            kl_scale_eval, kl_scale_est = plot_iteration_results(train_X, train_Y, test_X, y_pred_means, y_pred_vars,
                                                                 fix_model, i)

        acq_cand, acq_val, EI_val = acqmanEI(y_pred_means, y_pred_vars, train_Y)
        val = acq_val
        ind = np.random.choice(acq_cand)

        ################################################################
        ## Find next point which maximizes the learning through exploration-exploitation
        if (i == 1):
            val_ini = val
        # Check for convergence
        if ((val) < 0):  # Stop for negligible expected improvement
            print("Model converged due to sufficient learning over search space ")
            break
        else:
            nextX = torch.empty((1, len(X)))
            nextX_norm = torch.empty(1, len(X))
            nextX[0, :] = test_X[ind, :]
            nextX_norm[0, :] = test_X_norm[ind, :]

            # Evaluate true function for new data, augment data
            train_X, train_X_norm, train_Y, m = augment_newdata_KL(nextX, nextX_norm, train_X, train_X_norm, train_Y,
                                                                   fix_params, data, fix_model, m)

            # Gp model fit
            # Updating GP with augmented training data
            gp_surro = optimize_hyperparam_trainGP(train_X_norm, train_Y)

            # Saving/Updating data at each iterations
            np.save("train_X.npy", train_X)
            np.save("train_X_norm.npy", train_X_norm)
            np.save("train_Y.npy", train_Y)
            np.save("m.npy", m)

    ## Final posterior prediction after all the sampling done

    if (i == N):
        print("Max. sampling reached, model stopped")

    # Optimal GP learning
    gp_opt = gp_surro
    # Posterior calculation with converged GP model
    y_pred_means, y_pred_vars = cal_posterior(gp_opt, test_X_norm)
    # Plotting functions to check final iteration
    kl_scale_eval_opt, kl_scale_est_opt = plot_iteration_results(train_X, train_Y, test_X, y_pred_means, y_pred_vars,
                                                                 fix_model, i)

    return kl_scale_eval_opt, kl_scale_est_opt, gp_opt, train_X, train_Y


"""#Prepare a set of KL trajectories
Create the set of the possible trajectories. Here, we define trajectories from different functionals in real space.
With these, we 
- define the 2D latent space to sample from (i.e. pretrain)
- draw the point form the space and evaluate the jrVAE model
- build a BO framework in the reduced 2D latent space for KL factor optimization 
"""

# Prepare training data to fit trajectory in a VAE model

torch.manual_seed(100)
num_samples1 = 2500
num_samples2 = 2500
num_samples3 = 2500
# num_samples4 = 2000
num_traj = 120

#########Sampled trajectories defined from functional 1
traj_sampled1 = torch.empty((num_samples1, num_traj))
# Control parameters for defining KL trajectories -defined from functional 1
X_start = torch.linspace(50, 30, num_samples1)
X_stop = torch.linspace(5, 1, num_samples1)
X_coolrate = torch.linspace(80, 1, num_samples1)
X_timeout = torch.linspace(20, 1, num_samples1)

X = torch.vstack((X_start, X_stop, X_coolrate, X_timeout))
X = torch.transpose(X, 0, 1)
# print(X.shape)

for j in range(num_samples1):
    kl_scale_sampled = torch.cat(
        [torch.ones(int(np.round(X_timeout[j])), ) * X_start[j],
         # put pressure on the continuous latent channel at the beginning
         torch.linspace(X_start[j], X_stop[j], int(np.round(X_coolrate[j])))]  # gradually release the pressure
    )

    # Consider trajectory of 120 dimensions- defined by 4 variables (4D) in the real space
    for i in range(num_traj):
        traj_sampled1[j, i] = kl_scale_sampled[i] if i < len(kl_scale_sampled) else kl_scale_sampled[-1]

print(traj_sampled1.shape)

#########Sampled trajectories defined from functional 2
traj_sampled2, points, slopes = generate_1Dspectra_Segment(2, num_samples2)

# print(traj_sampled2.shape)
# Rescaling to similar sample data defined from func 1
traj_sampled2 = traj_sampled2.detach().numpy()

traj_sampled22 = np.reshape(traj_sampled2, num_samples2 * num_traj)
# print(traj_sampled2.shape, traj_sampled22.shape)

traj_sampled22_norm = (traj_sampled22 - np.min(traj_sampled22)) / (np.max(traj_sampled22) - np.min(traj_sampled22))
traj_sampled22_scaled = traj_sampled22_norm * (50 - 1) + 1
# print(traj_sampled22_scaled.shape, np.min(traj_sampled22_scaled), np.max(traj_sampled22_scaled))

traj_sampled22_scaled = np.reshape(traj_sampled22_scaled, (num_samples2, num_traj))
traj_sampled22_scaled = torch.from_numpy(traj_sampled22_scaled)
traj_sampled2 = torch.from_numpy(traj_sampled2)
# print(traj_sampled22_scaled.shape, traj_sampled2.shape)

####################Sampled trajs defined from functional 3
params = lambda: {"A": np.random.lognormal(1, 1),
                  "alpha": np.random.uniform(-2, 2),
                  "omega": np.random.uniform(8, 14),
                  "B": np.random.uniform(-1, 1)}

traj_sampled3 = np.zeros((num_samples3, num_traj))
n_traj3 = np.linspace(0, 2, num_traj)
for i in range(num_samples3):
    y = func_periodic(n_traj3, params())
    y = 2 * ((y - y.min()) / y.ptp()) - 1
    traj_sampled3[i, :] = y
# traj_sampled3 = np.array(traj_sampled3, dtype=np.float32)
print(traj_sampled3.shape)
# Rescaling to similar sample data defined from func 1

traj_sampled32 = np.reshape(traj_sampled3, num_samples3 * num_traj)

traj_sampled32_norm = (traj_sampled32 - np.min(traj_sampled32)) / (np.max(traj_sampled32) - np.min(traj_sampled32))
traj_sampled32_scaled = traj_sampled32_norm * (50 - 1) + 1
# print(traj_sampled32_scaled.shape, np.min(traj_sampled32_scaled), np.max(traj_sampled32_scaled))

traj_sampled32_scaled = np.reshape(traj_sampled32_scaled, (num_samples3, num_traj))
traj_sampled32_scaled = torch.from_numpy(traj_sampled32_scaled)
traj_sampled3 = torch.from_numpy(traj_sampled3)
print(traj_sampled32_scaled.shape, traj_sampled3.shape)

####################### Sampled traj from functional 4
# traj_sampled4 = torch.normal(0, 1, size=(num_samples4, num_traj))
# Rescaling to similar sample data defined from func 1
# traj_sampled4_scaled = traj_sampled4*(50-1) + 1
# print(traj_sampled4_scaled.shape, torch.min(traj_sampled4_scaled), torch.max(traj_sampled4_scaled))

# Combine data sampled from multiple functionals
traj_sampled = torch.vstack((traj_sampled1, traj_sampled22_scaled, traj_sampled32_scaled))

print(traj_sampled.shape)

# Plot the training data of sampled trajectories
num_samples = num_samples1 + num_samples2 + num_samples3
t = np.linspace(1, 120, 120)
fig, axes = plt.subplots(
    10, 10, figsize=(10, 10), subplot_kw={'xticks': [], 'yticks': []},
    gridspec_kw=dict(hspace=0.1, wspace=0.1))

for ax in axes.flat:
    i = np.random.randint(0, num_samples
                          )
    y_i = traj_sampled[i, :].detach().numpy()
    ax.plot(t, y_i)
    # ax.set_ylim (-1.1, 1.1)

plt.savefig('traj_sampled.png')

"""# Train the VAE model with sampled trajectories
-Here we convert the trajectories in a 2D latent space
"""

traj_sampled = traj_sampled.float()
train_loader_traj = pv.utils.init_dataloader(traj_sampled.unsqueeze(1), batch_size=64)

# set the dimension of the spectra
in_dim = (num_traj,)

# Initialize invariant VAE
vae_traj = pv.models.iVAE(in_dim, latent_dim=2, invariances=None,
                          sampler_d="gaussian", decoder_sig=.3, sigmoid_d=False,
                          seed=0)

# Initialize SVI trainer
trainer_traj = pv.trainers.SVItrainer(vae_traj, lr=1e-4)

# Train for n epochs:
for e in range(2000):
    trainer_traj.step(train_loader_traj)
    trainer_traj.print_statistics()

"""View the learned latent manifold:"""

vae_traj.manifold2d(d=10)
plt.savefig('vae_traj.manifold2d.png')

"""Encode the training data into the latent space:"""

train_data = train_loader_traj.dataset.tensors[0]
# train_data = traj_sampled
z_mean_traj, z_sd_traj = vae_traj.encode(train_data)
print(z_mean_traj.shape, z_sd_traj.shape)
plt.figure(figsize=(6, 6))
plt.scatter(z_mean_traj[:, -2], z_mean_traj[:, -1], s=10, alpha=0.15)
plt.xlabel("$z_1$", fontsize=14)
plt.ylabel("$z_2$", fontsize=14)
plt.savefig('TD_Latentspace.png')
# plt.show()

"""- Lets divide the latent space into feasible and infeasible region"""

z1_traj = np.linspace(torch.min(z_mean_traj[:, -2]), torch.max(z_mean_traj[:, -2]), 100)
z2_traj = np.linspace(torch.min(z_mean_traj[:, -1]), torch.max(z_mean_traj[:, -1]), 100)
z1_traj, z2_traj = np.meshgrid(z1_traj, z2_traj)
decoded_traj_feas = np.zeros((100, 100))
z = torch.empty((1, 2))
m = 1
for t1, (x1, x2) in enumerate(zip(z1_traj, z2_traj)):
    for t2, (xx1, xx2) in enumerate(zip(x1, x2)):
        # print("Evaluation # " +str(m))
        m = m + 1
        z[0, 0] = xx1
        z[0, 1] = xx2
        decoded_traj = vae_traj.decode(z).numpy()
        if (np.min(decoded_traj) > 0):
            decoded_traj_feas[t1, t2] = 1  # 1 denotes feasible
        else:
            decoded_traj_feas[t1, t2] = 0  # 0 denotes infeasible

print(decoded_traj_feas.shape)
print(np.sum(decoded_traj_feas))

# Plot the latent space and check feasible region
plt.figure()
plt.imshow(decoded_traj_feas, origin="lower")
a1 = (z_mean_traj[:, -2] - torch.min(z_mean_traj[:, -2])) / (
            torch.max(z_mean_traj[:, -2]) - torch.min(z_mean_traj[:, -2]))
a2 = (z_mean_traj[:, -1] - torch.min(z_mean_traj[:, -1])) / (
            torch.max(z_mean_traj[:, -1]) - torch.min(z_mean_traj[:, -1]))
b1 = a1 * (99 - 1) + 1
b2 = a2 * (99 - 1) + 1
plt.scatter(b1, b2, s=2, alpha=0.15)
plt.savefig('TD_Latentspace_feasible.png')

"""#Now we start Analysis- Graphene problem
- KL trajectory optimization using BO over the 2D latent space which decodes sample trajectory into real space.
- We run the BO with subset of data. This is to reduce the cost of function evaluation (Expensive) during BO since the VAE model cost increases with data size. We assume the optimal trajectory should not be dependent to the data size, given the data originates from same black-box model (Graphene data)
Get training data and create a dataloader object
Create a stack of submimages centered around a portion of the identified lattice atoms:
- Here we considered 600 images and window size 70 to build training data
- Added impurities
Add impurities
"""

# !pip install -U gdown
# !gdown "https://drive.google.com/uc?id=1mpecY83LV0sqDbsCzvGgBw4XUhSkiTqZ"
# gdown https://drive.google.com/uc?id=14o8Yb7mPyBhPrU14ymlr4j5pVCUYJUpq

train_data = np.load("train_data_imp.npy")
# print(train_data.shape)
train_data = torch.from_numpy(train_data)
print(train_data.shape)
train_data = train_data.float()

fig, axes = plt.subplots(10, 10, figsize=(8, 8),
                         subplot_kw={'xticks': [], 'yticks': []},
                         gridspec_kw=dict(hspace=0.1, wspace=0.1))

for ax, im in zip(axes.flat, train_data):
    ax.imshow(im.squeeze(), cmap='viridis', interpolation='nearest')

plt.savefig('TD_graphene.png')
plt.show()

"""#KL optimization using constrained BO over 2D latent space"""
print("Start optimization")
batch_size = 10
B = 12  # grid size for manifold2D
# Data dim size
H = 70
W = 70
# Initialize # of discrete class
discrete_dim = 10  # We dont have any prior knowledge with the actual # of discrete class of defects, we initialize arbitarily and changes which seems best fit with learning (classification) with VAE model
#kl_d = 3
#Initialize for BO
num_rows =100
num_start = 20  # Starting samples
N= 120

#latent parameters for defining KL trajectories
z1_traj = torch.linspace(torch.min(z_mean_traj[:, -2]), torch.max(z_mean_traj[:, -2]), num_rows)
z2_traj = torch.linspace(torch.min(z_mean_traj[:, -1]), torch.max(z_mean_traj[:, -1]), num_rows)

Z= torch.vstack((z1_traj, z2_traj))
#print(Z.shape[1])
#Fixed parameters of VAE model
fix_params = [batch_size, B, H, W, discrete_dim]
latent_model = vae_traj
#train_data_ss = train_data_ss.float()
#Z_feas = getfeasible(Z, latent_model)
kl_cont_eval_opt, kl_cont_est_opt, gp_opt, train_X, train_Y = latentBO_KL(Z, fix_params, train_data, latent_model, num_rows, num_start, N)

np.save("kl_cont_eval_opt.npy", kl_cont_eval_opt)
np.save("kl_cont_est_opt.npy", kl_cont_est_opt)
np.save("train_X_final.npy", train_X)
np.save("train_Y_final.npy", train_Y)