import numpy as np 
from scipy.optimize import fminbound
import itertools
from scipy.linalg import logm

# --- step 71.1 ---
def ket(dim, args):
    '''Input:
    dim: int or list, dimension of the ket
    args: int or list, the i-th basis vector (or list of basis indices)
    Output:
    out: the matrix representation of the ket, returned as a column vector of shape (D, 1) where D is the (product of) dimension(s)
    '''
    raise NotImplementedError

# --- step 71.2 ---
def tensor(*args):
    '''Takes the tensor product of an arbitrary number of matrices/vectors.
    Input:
    args: any number of nd arrays of floats, corresponding to input matrices/vectors
    Output:
    M: the tensor product (Kronecker product) of the inputs (np.kron applied left to right). The output keeps the dimensionality of the inputs: 1-D inputs yield a 1-D output, 2-D inputs yield a 2-D output.
    '''
    raise NotImplementedError

# --- step 71.3 ---
def apply_channel(K, rho, sys=None, dim=None):
    '''Applies the channel with Kraus operators in K to the state rho on
    systems specified by the list sys. The dimensions of the subsystems of
    rho are given by dim.
    Inputs:
    K: list of 2d array of floats, list of Kraus operators
    rho: 2d array of floats, input density matrix
    sys: list of int or None, list of subsystems to apply the channel (1-based indices), None means full system
    dim: list of int or None, list of dimensions of each subsystem, None means full system
    Output:
    matrix: output density matrix of floats
    '''
    raise NotImplementedError

# --- step 71.4 ---
def syspermute(X, perm, dim):
    '''Permutes order of subsystems in the multipartite operator X.
    Inputs:
    X: 2d array of floats with equal dimensions, the density matrix of the state
    perm: list of int, a 1-based permutation of 1..n giving the desired order; output subsystem k is input subsystem perm[k]
    dim: list of int containing the dimensions of all subsystems.
    Output:
    Y: 2d array of floats with equal dimensions, the density matrix of the permuted state
    '''
    raise NotImplementedError

# --- step 71.5 ---
def partial_trace(X, sys, dim):
    '''Inputs:
    X: 2d array of floats with equal dimensions, the density matrix of the state
    sys: list of int (1-based) containing systems over which to take the partial trace (i.e., the systems to discard).
    dim: list of int containing dimensions of all subsystems.
    Output:
    2d array of floats with equal dimensions, density matrix after partial trace.
    '''
    raise NotImplementedError

# --- step 71.6 ---
def entropy(rho):
    '''Inputs:
    rho: 2d array of floats with equal dimensions, the density matrix of the state
    Output:
    en: quantum (von Neumann) entropy of the state rho in bits (log base 2), float
    '''
    raise NotImplementedError

# --- step 71.7 ---
def generalized_amplitude_damping_channel(gamma, N):
    '''Generates the generalized amplitude damping channel.
    Inputs:
    gamma: float, damping parameter
    N: float, thermal parameter
    Output:
    kraus: list of Kraus operators as 2x2 arrays of floats, [A1, A2, A3, A4]
    '''
    raise NotImplementedError

# --- step 71.8 ---
def neg_rev_coh_info(p, g, N):
    '''Calculates the negative of the reverse coherent information of the output state
    Inputs:
    p: float, parameter for the input state
    g: float, damping parameter
    N: float, thermal parameter
    Outputs:
    neg_I_R: float, negative of the reverse coherent information of the output state
    '''
    raise NotImplementedError

# --- step 71.9 ---
def GADC_rev_coh_inf(g, N):
    '''Calculates the channel reverse coherent information of the GADC.
    Inputs:
    g: float, damping parameter
    N: float, thermal parameter
    Outputs:
    channel_rev_coh_info: float, channel reverse coherent information of a GADC
    '''
    raise NotImplementedError
