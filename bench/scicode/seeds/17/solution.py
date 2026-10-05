import sympy as sp
import numpy as np

# --- step 17.1 ---
def init_eji_array(energy, energy_vertices):
    '''Initialize and populate a 5x5 array of energy differences e_ij = eps_j - eps_i, and map
    them to sympy symbols for later evaluation. Here eps_0 = energy and eps_1,...,eps_4 = energy_vertices.
    Inputs:
    - energy: A float, the iso-surface energy level eps_0 for density of states integration.
    - energy_vertices: A list of 4 floats, the tetrahedron vertex energies eps_1,...,eps_4.
    Outputs:
    - symbols: A dictionary mapping each name string 'e{i}{j}' (i,j in 0..4, no separator, e.g. 'e01')
      to its sympy Symbol.
    - value_map: A dictionary mapping each sympy Symbol to its float value eps_j - eps_i
      (e.g. the symbol named 'e01' holds eps_1 - eps_0).
    '''
    raise NotImplementedError

# --- step 17.2 ---
def integrate_DOS(energy, energy_vertices):
    '''Input:
    energy: a float number representing the energy value at which the density of states will be integrated
    energy_vertices: a list of float numbers representing the energy values at the four vertices of a tetrahedron when implementing the linear tetrahedron method
    Output:
    result: a float number representing the integration results of the density of states
    '''
    raise NotImplementedError
