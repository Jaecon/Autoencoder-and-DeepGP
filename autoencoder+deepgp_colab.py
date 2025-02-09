# -*- coding: utf-8 -*-
"""autoencoder+deepgp_colab.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1QpjIw_BKCmtlV0Meg9YBGKGP85btug0Y
"""

!pip install gpytorch -q
!pip install datatable -q

# Commented out IPython magic to ensure Python compatibility.
# %set_env CUDA_VISIBLE_DEVICES=0
import torch
import torch.nn as nn
import torch.nn.functional as F
import tqdm
import gpytorch
from gpytorch.means import ConstantMean, LinearMean
from gpytorch.kernels import RBFKernel, ScaleKernel, MaternKernel
from gpytorch.variational import VariationalStrategy, CholeskyVariationalDistribution
from gpytorch.distributions import MultivariateNormal
from gpytorch.models import ApproximateGP, GP
from gpytorch.mlls import VariationalELBO, AddedLossTerm
from gpytorch.likelihoods import GaussianLikelihood
from gpytorch.models.deep_gps import DeepGPLayer, DeepGP
from gpytorch.mlls import DeepApproximateMLL

import datatable as dt
import numpy as np
import pandas as pd

import random

smoke_test = False
TRAINING = False

from google.colab import drive
drive.mount('/content/drive')

data = pd.read_csv('/content/drive/MyDrive/jane-street-market-prediction/train.csv')
data.fillna(data.mean(), inplace=True)
if TRAINING:
    if smoke_test:
        train = data.query('date > 85 & date <= 90')
    else:
        train = data.query('date > 85 & date < 450')
    train_x = torch.tensor(train[features].to_numpy()).type(torch.float32)
    train_y = torch.tensor(train[resp_cols].to_numpy()).type(torch.float32)
    if torch.cuda.is_available():
        train_x, train_y = train_x.cuda(), train_y.cuda()
    
    f_mean = np.mean(train[features[1:]].values, axis=0)

features = [c for c in data.columns if 'feature' in c]
resp_cols = ['resp_1', 'resp_2', 'resp_3', 'resp_4', 'resp']

from torch.utils.data import TensorDataset, DataLoader
if TRAINING:
    train_dataset = TensorDataset(train_x, train_y)
    train_loader = DataLoader(train_dataset, batch_size=256, shuffle=True)

"""### Autoencoder ###"""

class GaussianNoise(nn.Module):
    def __init__(self, stddev):
        super().__init__()
        self.stddev = stddev

    def forward(self, din):
        if self.training:
            return din + torch.autograd.Variable(torch.randn(din.size()).cuda() * self.stddev)
        return din

class AutoEncoder(nn.Module):
    def __init__(self,input_dim,output_dim,stddev=0.05):
        super(AutoEncoder, self).__init__()
        self.gaussian_noise = GaussianNoise(stddev=stddev)
        self.batch_norm1 = nn.BatchNorm1d(input_dim)
        self.batch_norm2 = nn.BatchNorm1d(32)
        self.fc1 = nn.Linear(input_dim, 64)
        self.fc2 = nn.Linear(64,input_dim)
        self.fc3 = nn.Linear(input_dim, 32)
        self.fc4 = nn.Linear(32, output_dim)

    def forward(self, input):
        encoded = self.batch_norm1(input)
        encoded = self.gaussian_noise(encoded)
        encoded = F.relu(self.fc1(encoded))
        
        decoded = F.dropout(encoded, p=0.2)
        decoded = self.fc2(decoded)

        x = F.relu(self.fc3(decoded))
        x = self.batch_norm2(x)
        x = F.dropout(x, p=0.2)
        x = torch.sigmoid(self.fc4(x))
        return decoded, x

ae = AutoEncoder(len(features), len(resp_cols), stddev=0.1)

if torch.cuda.is_available():
    ae = ae.cuda()
print(ae)

if TRAINING:
    ae.train()

    num_epochs = 100 

    optimizer = torch.optim.Adam([{'params': ae.parameters()}], lr=1e-3)

    epochs_iter = tqdm.notebook.tqdm(range(num_epochs), desc="Epoch")
    for i in epochs_iter:
        # Within each iteration, we will go over each minibatch of data
        minibatch_iter = tqdm.notebook.tqdm(train_loader, desc="Minibatch", leave=False)
        for x_batch, y_batch in minibatch_iter:
            optimizer.zero_grad()
            decoded, x = ae(x_batch)
            new_y_batch = (y_batch > 0).type(torch.float32) 
            loss = nn.MSELoss()(decoded, x_batch) + nn.BCEWithLogitsLoss()(x, new_y_batch)
            loss.backward()
            optimizer.step()
            minibatch_iter.set_postfix(loss=loss.item())                    
        torch.cuda.empty_cache()
        
    torch.save(ae.state_dict(), '/content/drive/MyDrive/jane-street-market-prediction/ae.pth')
    
else:
    state_dict = torch.load('/content/drive/MyDrive/jane-street-market-prediction/ae.pth')
    ae.load_state_dict(state_dict)

#ae.state_dict()['batch_norm1.weight']

"""### DeepGP ###"""

num_inducing = 128
num_samples = 50

class DeepGPHiddenLayer(DeepGPLayer):
    def __init__(self, input_dims, output_dims, num_inducing=num_inducing, mean_type='constant'):
        if output_dims is None:
            inducing_points = torch.randn(num_inducing, input_dims)
            batch_shape = torch.Size([])
        else:
            inducing_points = torch.randn(output_dims, num_inducing, input_dims)
            batch_shape = torch.Size([output_dims])

        variational_distribution = CholeskyVariationalDistribution(
            num_inducing_points=num_inducing,
            batch_shape=batch_shape
        )

        variational_strategy = VariationalStrategy(
            self,
            inducing_points,
            variational_distribution,
            learn_inducing_locations=True
        )

        super(DeepGPHiddenLayer, self).__init__(variational_strategy, input_dims, output_dims)

        if mean_type == 'constant':
            self.mean_module = ConstantMean(batch_shape=batch_shape)
        else:
            self.mean_module = LinearMean(input_dims)
        self.covar_module = ScaleKernel(
            RBFKernel(batch_shape=batch_shape, ard_num_dims=input_dims),
            batch_shape=batch_shape, ard_num_dims=None
        )

    def forward(self, x):
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return MultivariateNormal(mean_x, covar_x)

    def __call__(self, x, *other_inputs, **kwargs):
        """
        Overriding __call__ isn't strictly necessary, but it lets us add concatenation based skip connections
        easily. For example, hidden_layer2(hidden_layer1_outputs, inputs) will pass the concatenation of the first
        hidden layer's outputs and the input data to hidden_layer2.
        """
        if len(other_inputs):
            if isinstance(x, gpytorch.distributions.MultitaskMultivariateNormal):
                x = x.rsample()

            processed_inputs = [
                inp.unsqueeze(0).expand(gpytorch.settings.num_likelihood_samples(num_samples).value(), *inp.shape)
                for inp in other_inputs
            ]

            x = torch.cat([x] + processed_inputs, dim=-1)

        return super().__call__(x, are_samples=bool(len(other_inputs)))

# TWO hidden layers
num_output_dims1 = 16
num_output_dims2 = 8
ae_output_shape = len(features) + len(resp_cols)

class DeepGP(DeepGP):
    def __init__(self, input_shape):
        super().__init__()
        hidden_layer1 = DeepGPHiddenLayer(
            input_dims=input_shape,
            output_dims=num_output_dims1,
            mean_type='linear',
        )
        
        hidden_layer2 = DeepGPHiddenLayer(
            input_dims=hidden_layer1.output_dims,
            output_dims=num_output_dims2,
            mean_type='linear',
        )
        
        last_layer = DeepGPHiddenLayer(
            input_dims=hidden_layer2.output_dims,
            output_dims=None,
            mean_type='constant',
        )
         
        self.feature_extractor = torch.nn.Sequential(
            torch.nn.Linear(ae_output_shape, 65),
            torch.nn.BatchNorm1d(65),
            torch.nn.ReLU(),
            torch.nn.Linear(65, 32),
            torch.nn.BatchNorm1d(32),
            torch.nn.ReLU(),
        )

        
        self.hidden_layer1 = hidden_layer1
        self.hidden_layer2 = hidden_layer2
        self.last_layer = last_layer
        self.likelihood = GaussianLikelihood()

        
    def forward(self, inputs):
        reduced_inputs = self.feature_extractor(inputs)
        hidden_rep1 = self.hidden_layer1(reduced_inputs)
        hidden_rep2 = self.hidden_layer2(hidden_rep1)
        output = self.last_layer(hidden_rep2)
        return output

model = DeepGP(32)
if torch.cuda.is_available():
    model = model.cuda()

#TRAINING = True

if TRAINING:
    # this is for running the notebook in our testing framework
    num_epochs = 30 

    model.train()
    ae.eval()

    optimizer = torch.optim.Adam([{'params': model.parameters()}], lr=1e-3)

    mll = DeepApproximateMLL(VariationalELBO(model.likelihood, model, train_x.shape[-2]))

    epochs_iter = tqdm.notebook.tqdm(range(num_epochs), desc="Epoch")
    for i in epochs_iter:
        # Within each iteration, we will go over each minibatch of data
        minibatch_iter = tqdm.notebook.tqdm(train_loader, desc="Minibatch", leave=False)
        for x_batch, y_batch in minibatch_iter:
            with gpytorch.settings.num_likelihood_samples(num_samples):
                optimizer.zero_grad()
                decoded, x = ae(x_batch)
                new_x_batch = torch.cat((decoded, x), dim=-1).cuda()
                new_y_batch = y_batch[:,-1]
                output = model(new_x_batch)
                loss = -mll(output, new_y_batch)
                loss.backward()
                optimizer.step()
                minibatch_iter.set_postfix(loss=loss.item())        

        torch.cuda.empty_cache()

    torch.save(model.state_dict(), '/content/drive/MyDrive/jane-street-market-prediction/model.pth')
else:
    state_dict = torch.load('/content/drive/MyDrive/jane-street-market-prediction/model.pth')
    model.load_state_dict(state_dict)

#model.state_dict()

if smoke_test:
    test = data.query('date == 90')
else:
    test = data.query('date >= 450')

test_x = torch.tensor(test[features].to_numpy()).type(torch.float32)
test_y = torch.tensor(test[resp_cols].to_numpy()).type(torch.float32)
if torch.cuda.is_available:
    test_x, test_y = test_x.cuda(), test_y.cuda()

test_dataset = TensorDataset(test_x, test_y)
test_loader = DataLoader(test_dataset, batch_size = 128)

if TRAINING:
    del train, train_x, train_y

ae.eval()
model.eval()

new_test_x=torch.Tensor().cuda()
for x_batch, y_batch in tqdm.notebook.tqdm(test_loader):
    with torch.no_grad():
        decoded, x = ae(x_batch)
        res = torch.cat((decoded, x), dim=-1)
        new_test_x = torch.cat((new_test_x, res), dim=0)
        torch.cuda.empty_cache()

new_test_x.shape

new_test_x_loader = DataLoader(new_test_x, batch_size=128)

mus = []
variances = []
for new_x_batch in tqdm.notebook.tqdm(new_test_x_loader):
    with torch.no_grad(), gpytorch.settings.num_likelihood_samples(num_samples):
        pred = model.likelihood(model(new_x_batch))
        mus.append(pred.mean)
        variances.append(pred.variance)
        torch.cuda.empty_cache()

pred_mus, pred_vars = torch.cat(mus, dim=-1), torch.cat(variances, dim=-1)
pred_mus = pred_mus.mean(0).cpu().detach().numpy()
pred_vars = pred_vars.mean(0).cpu().detach().numpy() 

resp_sampling = np.random.normal(pred_mus, np.sqrt(pred_vars))
action = np.where(resp_sampling > 0., 1, 0).astype(int)

new_test_y = np.where(test_y.cpu().detach().numpy() > 0., 1, 0).astype(int)

errors = np.sum(action == new_test_y[:,-1]) / len(new_test_y)

errors

score = np.expand_dims(test.weight, axis=-1) * np.expand_dims(test_y[:,-1].cpu().numpy(), axis=-1) * np.expand_dims(action, axis=-1)

df_for_eval = pd.DataFrame(
    np.concatenate( (np.expand_dims(test.date, axis = -1), score), axis=-1 ), 
    columns=["date",'score']
    )

p = df_for_eval.groupby(['date']).sum()

t = (np.sum(p.score) / np.sqrt(np.sum(np.square(p.score)))) * np.sqrt(250 / len(p))
t

# Utility
np.min( [np.max([t,0]), 6] ) * np.sum(p.score)

# Possible Maximum Utility
resp = np.expand_dims(test_y[:,-1].cpu().numpy(), axis=-1)
best_score = np.expand_dims(test.weight, axis=-1) * resp * (resp>0).astype(int)

df_best = pd.DataFrame(
    np.concatenate( (np.expand_dims(test.date, axis = -1), best_score), axis=-1 ), 
    columns=["date",'best_score']
    )
p_best = df_best.groupby(['date']).sum()
t_best = (np.sum(p_best.best_score) / np.sqrt(np.sum(np.square(p_best.best_score)))) * np.sqrt(250 / len(p_best))

np.min( [np.max([t_best,0]), 6] ) * np.sum(p_best.best_score)

#import janestreet
#from scipy.stats import norm
#
#env = janestreet.make_env() # initialize the environment
#iter_test = env.iter_test() # an iterator which loops over the test set
#
#model.eval()
#ae.eval()
#
#for (test_df, pred_df) in tqdm.notebook.tqdm(iter_test):
#    if test_df['weight'].item() > 0:
#        x_tt = test_df.loc[:, features].values
#        if np.isnan(x_tt[:, 1:].sum()):
#            x_tt[:, 1:] = np.nan_to_num(x_tt[:, 1:]) + np.isnan(x_tt[:, 1:]) * f_mean
#        x_tt = torch.tensor(x_tt).type(torch.float32).cuda()
#        
#        mus = []
#        variances = []
#        with torch.no_grad() and gpytorch.settings.num_likelihood_samples(num_samples):
#            optimizer.zero_grad()
#            decoded, x = ae(x_tt)
#            new_test_df = torch.cat((decoded, x), dim=-1).cuda()
#            preds = model.likelihood(model(new_test_df))
#            mus.append(preds.mean)
#            variances.append(preds.variance)
#        pred_mus, pred_vars = torch.cat(mus, dim=-1), torch.cat(variances, dim=-1)
#        pred = norm.cdf(0, pred_mus.mean(0).cpu().detach().numpy(), pred_vars.mean(0).cpu().detach().numpy())
#        pred_df.action = np.where(pred <= 0.20, 1, 0).astype(int)
#    else:
#        pred_df.action = 0
#    
#    env.predict(pred_df)