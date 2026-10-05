import numpy as np

# --- step 40.1 ---
def second_diff(target, u, dx):
    '''Inputs:
    target : Target cell index, int
    u      : Approximated solution value, array of floats with minimum length of 5
    dx     : Spatial interval, float
    Outputs:
    deriv  : Second order derivative of the given target cell, float
    '''
    raise NotImplementedError

# --- step 40.2 ---
def Strang_splitting(u, dt, dx, alpha):
    '''Inputs:
    u : solution, array of float
    dt: time interval , float
    dx: sptial interval, float
    alpha: diffusive coefficient, float
    Outputs:
    u : solution, array of float
    '''
    raise NotImplementedError

# --- step 40.3 ---
def solve(CFL, T, dt, alpha):
    '''Inputs:
    CFL : Courant-Friedrichs-Lewy condition number
    T   : Max time, float
    dt  : Time interval, float
    alpha : diffusive coefficient , float
    Outputs:
    u   : solution, array of float
    '''
    raise NotImplementedError
