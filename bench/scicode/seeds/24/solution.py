import numpy as np

# --- step 24.1 ---
def make_IC(n):
    '''The function computes the inital condition mentioned above
    Inputs:
    n  : number of grid points, integer
    Outputs:
    v  : cell averaged approximation of initial condition, 1d array size n-1
    '''
    raise NotImplementedError

# --- step 24.2 ---
def LaxF(uL, uR):
    '''This function computes Lax-Fridrich numerical flux.
    Inputs: 
    uL : Cell averaged value at cell i, float
    uR : Cell averaged value at cell i+1, float
    Output: flux, float
    '''
    raise NotImplementedError

# --- step 24.3 ---
def solve(n_x, n_t, T):
    '''Inputs:
    n_x : number of spatial grids, Integer
    n_t : number of temporal grid points (n_t - 1 time intervals), Integer
    T   : final time, float
    Outputs
    u1   : solution vector, 1d array of size n_x-1
    '''
    raise NotImplementedError
