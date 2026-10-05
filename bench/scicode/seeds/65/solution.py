import numpy as np
from scipy.linalg import sqrtm
import itertools

# --- step 65.1 ---
def tensor(*args):
    '''Takes the tensor product of an arbitrary number of matrices/vectors.
    Input:
    args: any number of nd arrays of floats, corresponding to input matrices
    Output:
    M: the tensor (Kronecker) product of the inputs - a 1-d array when all inputs are 1-d, otherwise 2-d - array of floats or complex
    '''
    raise NotImplementedError

# --- step 65.2 ---
def apply_channel(K, rho, sys=None, dim=None):
    '''Applies the channel with Kraus operators in K to the state rho on
    systems specified by the list sys. The dimensions of the subsystems of
    rho are given by dim.
    Inputs:
    K: list of 2d array of floats or complex, list of Kraus operators
    rho: 2d array of floats, input density matrix
    sys: list of int or None, 1-based indices of the subsystems the channel acts on (the first subsystem is 1); None means the channel acts on the full system
    dim: list of int or None, list of dimensions of each subsystem, None means full system
    Output:
    matrix: output density matrix of floats
    '''
    raise NotImplementedError

# --- step 65.3 ---
def channel_output(input_state, channel1, channel2=None):
    '''Returns the channel output
    Inputs:
        input_state: density matrix of the input 2n qubit state, ( 2**(2n), 2**(2n) ) array of floats
        channel1: kruas operators of the first channel, list of (2,2) array of floats or complex
        channel2: kruas operators of the second channel, list of (2,2) array of floats or complex
    Output:
        output: the channel output, ( 2**(2n), 2**(2n) ) array of floats
    '''
    raise NotImplementedError

# --- step 65.4 ---
def ghz_protocol(state):
    '''Returns the output state of the protocol
    Input:
    state: 2n qubit input state, 2^2n by 2^2n array of floats, where n is determined from the size of the input state
    Output:
    post_selected: the output state
    '''
    raise NotImplementedError

# --- step 65.5 ---
def fidelity(rho, sigma):
    '''Returns the fidelity between two states.
    Inputs:
    rho, sigma: density matrices of the two states, 2d array of floats
    Output:
    fid: fidelity, float
    '''
    raise NotImplementedError

# --- step 65.6 ---
def ghz_protocol_fidelity(input_state, channel1, channel2=None):
    '''Returns the achievable fidelity of the protocol
    Inputs:
        input_state: density matrix of the input 2n qubit state, ( 2**(2n), 2**(2n) ) array of floats
        channel1: kruas operators of the first channel, list of (2,2) array of floats or complex
        channel2: kruas operators of the second channel, list of (2,2) array of floats or complex
    Output:
        fid: achievable fidelity of protocol, float
    '''
    raise NotImplementedError
