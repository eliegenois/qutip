# This file is part of QuTiP: Quantum Toolbox in Python.
#
#    Copyright (c) 2011 and later, Paul D. Nation and Robert J. Johansson.
#    All rights reserved.
#
#    Redistribution and use in source and binary forms, with or without
#    modification, are permitted provided that the following conditions are
#    met:
#
#    1. Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#
#    2. Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
#    3. Neither the name of the QuTiP: Quantum Toolbox in Python nor the names
#       of its contributors may be used to endorse or promote products derived
#       from this software without specific prior written permission.
#
#    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#    "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#    LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
#    PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#    HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#    SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#    LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#    DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#    THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#    (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#    OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
###############################################################################
"""
Module contains functions for solving for the steady state density matrix of
open quantum systems defined by a Liouvillian or Hamiltonian and a list of
collapse operators.
"""

__all__ = ['steadystate', 'steady', 'build_preconditioner']

import warnings
import time
import scipy
import numpy as np
from numpy.linalg import svd
from scipy import prod
import scipy.sparse as sp
import scipy.linalg as la
from scipy.sparse.linalg import (use_solver, splu, spilu, spsolve, eigs,
                                 LinearOperator, gmres, lgmres, bicgstab)
from qutip.qobj import Qobj, issuper, isoper
from qutip.superoperator import liouvillian, vec2mat
from qutip.sparse import sp_permute, sp_bandwidth, sp_reshape, sp_profile
from qutip.graph import reverse_cuthill_mckee, weighted_bipartite_matching
import qutip.settings as settings
from qutip.utilities import _version2int
import qutip.logging
import inspect
logger = qutip.logging.get_logger()
logger.setLevel('DEBUG')

# test if scipy is recent enought to get L & U factors from superLU
_scipy_check = _version2int(scipy.__version__) >= _version2int('0.14.0')


def _default_steadystate_args():
    def_args = {'method': 'direct', 'sparse': True, 'use_rcm': False,
                'use_wbm': False, 'use_umfpack': False, 'weight': None,
                'use_precond': False, 'all_states': False,
                'M': None, 'x0': None, 'drop_tol': 1e-4, 'fill_factor': 100,
                'diag_pivot_thresh': None, 'maxiter': 1000, 'tol': 1e-9,
                'permc_spec': 'COLAMD', 'ILU_MILU': 'smilu_2', 'restart': 20,
                'return_info': False, 'info': {'perm': []}}

    return def_args


def steadystate(A, c_op_list=[], **kwargs):
    """Calculates the steady state for quantum evolution subject to the
    supplied Hamiltonian or Liouvillian operator and (if given a Hamiltonian) a
    list of collapse operators.

    If the user passes a Hamiltonian then it, along with the list of collapse
    operators, will be converted into a Liouvillian operator in Lindblad form.

    Parameters
    ----------
    A : qobj
        A Hamiltonian or Liouvillian operator.

    c_op_list : list
        A list of collapse operators.

    method : str {'direct', 'eigen', 'iterative-gmres',
                  'iterative-lgmres', 'iterative-bicgstab', 'svd', 'power'}
        Method for solving the underlying linear equation. Direct LU solver
        'direct' (default), sparse eigenvalue problem 'eigen',
        iterative GMRES method 'iterative-gmres', iterative LGMRES method
        'iterative-lgmres', iterative BICGSTAB method 'iterative-bicgstab',
         SVD 'svd' (dense), or inverse-power method 'power'.

    return_info : bool, optional, default = False
        Return a dictionary of solver-specific infomation about the
        solution and how it was obtained.

    sparse : bool, optional, default = True
        Solve for the steady state using sparse algorithms. If set to False,
        the underlying Liouvillian operator will be converted into a dense
        matrix. Use only for 'smaller' systems.

    use_rcm : bool, optional, default = False
        Use reverse Cuthill-Mckee reordering to minimize fill-in in the
        LU factorization of the Liouvillian.

    use_wbm : bool, optional, default = False
        Use Weighted Bipartite Matching reordering to make the Liouvillian
        diagonally dominant.  This is useful for iterative preconditioners
        only, and is set to ``True`` by default when finding a preconditioner.

    weight : float, optional
        Sets the size of the elements used for adding the unity trace condition
        to the linear solvers.  This is set to the average abs value of the
        Liouvillian elements if not specified by the user.

    use_umfpack : bool {False, True}
        Use umfpack solver instead of SuperLU.  For SciPy 0.14+, this option
        requires installing scikits.umfpack.

    x0 : ndarray, optional
        ITERATIVE ONLY. Initial guess for solution vector.

    maxiter : int, optional, default=1000
        ITERATIVE ONLY. Maximum number of iterations to perform.

    tol : float, optional, default=1e-9
        ITERATIVE ONLY. Tolerance used for terminating solver.

    permc_spec : str, optional, default='COLAMD'
        ITERATIVE ONLY. Column ordering used internally by superLU for the
        'direct' LU decomposition method. Options include 'COLAMD' and
        'NATURAL'. If using RCM then this is set to 'NATURAL' automatically
        unless explicitly specified.

    use_precond : bool optional, default = False
        ITERATIVE ONLY. Use an incomplete sparse LU decomposition as a
        preconditioner for the 'iterative' GMRES and BICG solvers.
        Speeds up convergence time by orders of magnitude in many cases.

    M : {sparse matrix, dense matrix, LinearOperator}, optional
        ITERATIVE ONLY. Preconditioner for A. The preconditioner should
        approximate the inverse of A. Effective preconditioning can
        dramatically improve the rate of convergence for iterative methods.
        If no preconditioner is given and ``use_precond = True``, then one
        is generated automatically.

    fill_factor : float, optional, default = 100
        ITERATIVE ONLY. Specifies the fill ratio upper bound (>=1) of the iLU
        preconditioner.  Lower values save memory at the cost of longer
        execution times and a possible singular factorization.

    drop_tol : float, optional, default = 1e-4
        ITERATIVE ONLY. Sets the threshold for the magnitude of preconditioner
        elements that should be dropped.  Can be reduced for a courser
        factorization at the cost of an increased number of iterations, and a
        possible singular factorization.

    diag_pivot_thresh : float, optional, default = None
        ITERATIVE ONLY. Sets the threshold between [0,1] for which diagonal
        elements are considered acceptable pivot points when using a
        preconditioner.  A value of zero forces the pivot to be the diagonal
        element.

    ILU_MILU : str, optional, default = 'smilu_2'
        ITERATIVE ONLY. Selects the incomplete LU decomposition method
        algoithm used in creating the preconditoner. Should only be used by
        advanced users.

    Returns
    -------
    dm : qobj
        Steady state density matrix.

    info : dict, optional
        Dictionary containing solver-specific information about the solution.

    Notes
    -----
    The SVD method works only for dense operators (i.e. small systems).

    """
    ss_args = _default_steadystate_args()
    for key in kwargs.keys():
        if key in ss_args.keys():
            ss_args[key] = kwargs[key]
        else:
            raise Exception(
                "Invalid keyword argument '"+key+"' passed to steadystate.")

    # Set column perm to NATURAL if using RCM and not specified by user
    if ss_args['use_rcm'] and ('permc_spec' not in kwargs.keys()):
        ss_args['permc_spec'] = 'NATURAL'

    # Create & check Liouvillian
    A = _steadystate_setup(A, c_op_list)

    # Set weight parameter to avg abs val in L if not set explicitly
    if 'weight' not in kwargs.keys():
        ss_args['weight'] = np.mean(np.abs(A.data.data.max()))
        ss_args['info']['weight'] = ss_args['weight']

    if ss_args['method'] == 'direct':
        if ss_args['sparse']:
            return _steadystate_direct_sparse(A, ss_args)
        else:
            return _steadystate_direct_dense(A, ss_args)

    elif ss_args['method'] == 'eigen':
        return _steadystate_eigen(A, ss_args)

    elif ss_args['method'] in ['iterative-gmres',
                               'iterative-lgmres', 'iterative-bicgstab']:
        return _steadystate_iterative(A, ss_args)

    elif ss_args['method'] == 'svd':
        return _steadystate_svd_dense(A, ss_args)

    elif ss_args['method'] == 'power':
        return _steadystate_power(A, ss_args)

    else:
        raise ValueError('Invalid method argument for steadystate.')


def _steadystate_setup(A, c_op_list):
    """Build Liouvillian (if necessary) and check input.
    """
    n_op = len(c_op_list)

    if isoper(A):
        if n_op == 0:
            raise TypeError('Cannot calculate the steady state for a ' +
                            'non-dissipative system ' +
                            '(no collapse operators given)')
        else:
            A = liouvillian(A, c_op_list)
    if not issuper(A):
        raise TypeError('Solving for steady states requires ' +
                        'Liouvillian (super) operators')
    return A


def _steadystate_LU_liouvillian(L, ss_args):
    """Creates modified Liouvillian for LU based SS methods.
    """
    perm = None
    perm2 = None
    rev_perm = None
    n = prod(L.dims[0][0])
    L = L.data.tocsc() + sp.csc_matrix(
        (ss_args['weight']*np.ones(n), (np.zeros(n), [nn * (n + 1)
         for nn in range(n)])),
        shape=(n ** 2, n ** 2))

    if settings.debug:
        old_band = sp_bandwidth(L)[0]
        old_pro = sp_profile(L)[0]
        logger.debug('Orig. NNZ: %i' % L.nnz)
        if ss_args['use_rcm']:
            logger.debug('Original bandwidth: %i' % old_band)

    if ss_args['use_wbm']:
        if settings.debug:
            logger.debug('Calculating Weighted Bipartite Matching ordering...')
        _wbm_start = time.time()
        perm = weighted_bipartite_matching(L)
        _wbm_end = time.time()
        L = sp_permute(L, perm, [], 'csc')
        ss_args['info']['perm'].append('wbm')
        ss_args['info']['wbm_time'] = _wbm_end-_wbm_start
        if settings.debug:
            wbm_band = sp_bandwidth(L)[0]
            logger.debug('WBM bandwidth: %i' % wbm_band)

    if ss_args['use_rcm']:
        if settings.debug:
            logger.debug('Calculating Reverse Cuthill-Mckee ordering...')
        _rcm_start = time.time()
        perm2 = reverse_cuthill_mckee(L)
        _rcm_end = time.time()
        rev_perm = np.argsort(perm2)
        L = sp_permute(L, perm2, perm2, 'csc')
        ss_args['info']['perm'].append('rcm')
        ss_args['info']['rcm_time'] = _rcm_end-_rcm_start
        if settings.debug:
            rcm_band = sp_bandwidth(L)[0]
            rcm_pro = sp_profile(L)[0]
            logger.debug('RCM bandwidth: %i' % rcm_band)
            logger.debug('Bandwidth reduction factor: %f' % round(
                old_band/rcm_band, 1))
            logger.debug('Profile reduction factor: %f' % round(
                old_pro/rcm_pro, 1))
    L.sort_indices()
    return L, perm, perm2, rev_perm, ss_args


def steady(L, maxiter=10, tol=1e-6, itertol=1e-5, method='solve',
           use_umfpack=False, use_precond=False):
    """
    Deprecated. See steadystate instead.
    """
    message = "steady has been deprecated, use steadystate instead"
    warnings.warn(message, DeprecationWarning)
    return steadystate(L, [], maxiter=maxiter, tol=tol,
                       use_umfpack=use_umfpack, use_precond=use_precond)


def _steadystate_direct_sparse(L, ss_args):
    """
    Direct solver that uses scipy sparse matrices
    """
    if settings.debug:
        logger.debug('Starting direct LU solver.')

    dims = L.dims[0]
    n = prod(L.dims[0][0])
    b = np.zeros(n ** 2, dtype=complex)
    b[0] = ss_args['weight']

    L, perm, perm2, rev_perm, ss_args = _steadystate_LU_liouvillian(L, ss_args)
    if np.any(perm):
        b = b[np.ix_(perm,)]
    if np.any(perm2):
        b = b[np.ix_(perm2,)]

    use_solver(assumeSortedIndices=True, useUmfpack=ss_args['use_umfpack'])
    ss_args['info']['permc_spec'] = ss_args['permc_spec']
    ss_args['info']['drop_tol'] = ss_args['drop_tol']
    ss_args['info']['diag_pivot_thresh'] = ss_args['diag_pivot_thresh']
    ss_args['info']['fill_factor'] = ss_args['fill_factor']
    ss_args['info']['ILU_MILU'] = ss_args['ILU_MILU']

    if not ss_args['use_umfpack']:
        # Use superLU solver
        orig_nnz = L.nnz
        _direct_start = time.time()
        lu = splu(L, permc_spec=ss_args['permc_spec'],
                  diag_pivot_thresh=ss_args['diag_pivot_thresh'],
                  options=dict(ILU_MILU=ss_args['ILU_MILU']))
        v = lu.solve(b)
        _direct_end = time.time()
        ss_args['info']['solution_time'] = _direct_end-_direct_start
        if (settings.debug or ss_args['return_info']) and _scipy_check:
            L_nnz = lu.L.nnz
            U_nnz = lu.U.nnz
            ss_args['info']['l_nnz'] = L_nnz
            ss_args['info']['u_nnz'] = U_nnz
            ss_args['info']['lu_fill_factor'] = (L_nnz+U_nnz)/L.nnz
            if settings.debug:
                logger.debug('L NNZ: %i ; U NNZ: %i' % (L_nnz,U_nnz))
                logger.debug('Fill factor: %f' % ((L_nnz+U_nnz)/orig_nnz))

    else:
        # Use umfpack solver
        _direct_start = time.time()
        v = spsolve(L, b)
        _direct_end = time.time()
        ss_args['info']['solution_time'] = _direct_end-_direct_start
    
    if ss_args['return_info']:
        ss_args['info']['residual_norm'] = la.norm(b - L*v, np.inf)
    
    if (not ss_args['use_umfpack']) and ss_args['use_rcm']:
        v = v[np.ix_(rev_perm,)]

    data = vec2mat(v)
    data = 0.5 * (data + data.conj().T)
    if ss_args['return_info']:
        return Qobj(data, dims=dims, isherm=True), ss_args['info']
    else:
        return Qobj(data, dims=dims, isherm=True)


def _steadystate_direct_dense(L, ss_args):
    """
    Direct solver that use numpy dense matrices. Suitable for
    small system, with a few states.
    """
    if settings.debug:
        logger.debug('Starting direct dense solver.')

    dims = L.dims[0]
    n = prod(L.dims[0][0])
    b = np.zeros(n ** 2)
    b[0] = ss_args['weight']

    L = L.data.todense()
    L[0, :] = np.diag(ss_args['weight']*np.ones(n)).reshape((1, n ** 2))
    _dense_start = time.time()
    v = np.linalg.solve(L, b)
    _dense_end = time.time()
    ss_args['info']['solution_time'] = _dense_end-_dense_start
    if ss_args['return_info']:
        ss_args['info']['residual_norm'] = la.norm(b - L*v, np.inf)
    data = vec2mat(v)
    data = 0.5 * (data + data.conj().T)

    return Qobj(data, dims=dims, isherm=True)


def _steadystate_eigen(L, ss_args):
    """
    Internal function for solving the steady state problem by
    finding the eigenvector corresponding to the zero eigenvalue
    of the Liouvillian using ARPACK.
    """
    ss_args['info'].pop('weight', None)
    if settings.debug:
        logger.debug('Starting Eigen solver.')

    dims = L.dims[0]
    L = L.data.tocsc()

    if ss_args['use_rcm']:
        ss_args['info']['perm'].append('rcm')
        if settings.debug:
            old_band = sp_bandwidth(L)[0]
            logger.debug('Original bandwidth: %i' % old_band)
        perm = reverse_cuthill_mckee(L)
        rev_perm = np.argsort(perm)
        L = sp_permute(L, perm, perm, 'csc')
        if settings.debug:
            rcm_band = sp_bandwidth(L)[0]
            logger.debug('RCM bandwidth: %i' % rcm_band)
            logger.debug('Bandwidth reduction factor: %f' 
                            % round(old_band/rcm_band, 1))

    _eigen_start = time.time()
    eigval, eigvec = eigs(L, k=1, sigma=1e-15, tol=ss_args['tol'],
                          which='LM', maxiter=ss_args['maxiter'])
    _eigen_end = time.time()
    ss_args['info']['solution_time'] = _eigen_end - _eigen_start
    if ss_args['return_info']:
        ss_args['info']['residual_norm'] = la.norm(L*eigvec, np.inf)
    if ss_args['use_rcm']:
        eigvec = eigvec[np.ix_(rev_perm,)]

    data = vec2mat(eigvec)
    data = 0.5 * (data + data.conj().T)
    out = Qobj(data, dims=dims, isherm=True)
    if ss_args['return_info']:
        return out/out.tr(), ss_args['info']
    else:
        return out/out.tr()


def _iterative_precondition(A, n, ss_args):
    """
    Internal function for preconditioning the steadystate problem for use
    with iterative solvers.
    """
    if settings.debug:
        logger.debug('Starting preconditioner.')
    _precond_start = time.time()
    try:
        P = spilu(A, permc_spec=ss_args['permc_spec'],
                  drop_tol=ss_args['drop_tol'],
                  diag_pivot_thresh=ss_args['diag_pivot_thresh'],
                  fill_factor=ss_args['fill_factor'],
                  options=dict(ILU_MILU=ss_args['ILU_MILU']))

        P_x = lambda x: P.solve(x)
        M = LinearOperator((n ** 2, n ** 2), matvec=P_x)
        _precond_end = time.time()
        ss_args['info']['permc_spec'] = ss_args['permc_spec']
        ss_args['info']['drop_tol'] = ss_args['drop_tol']
        ss_args['info']['diag_pivot_thresh'] = ss_args['diag_pivot_thresh']
        ss_args['info']['fill_factor'] = ss_args['fill_factor']
        ss_args['info']['ILU_MILU'] = ss_args['ILU_MILU']
        ss_args['info']['precond_time'] = _precond_end-_precond_start

        if settings.debug or ss_args['return_info']:
            if settings.debug:
                logger.debug('Preconditioning succeeded.')
                logger.debug('Precond. time: %f' % (_precond_end-_precond_start))
            if _scipy_check:
                L_nnz = P.L.nnz
                U_nnz = P.U.nnz
                ss_args['info']['l_nnz'] = L_nnz
                ss_args['info']['u_nnz'] = U_nnz
                ss_args['info']['ilu_fill_factor'] = (L_nnz+U_nnz)/A.nnz
                e = np.ones(n ** 2, dtype=int)
                condest = la.norm(M*e, np.inf)
                ss_args['info']['ilu_condest'] = condest
                if settings.debug:
                    logger.debug('L NNZ: %i ; U NNZ: %i' % (L_nnz,U_nnz))
                    logger.debug('Fill factor: %f' % ((L_nnz+U_nnz)/A.nnz))
                    logger.debug('iLU condest: %f' % condest)

    except:
        raise Exception("Failed to build preconditioner. Try increasing " +
                        "fill_factor and/or drop_tol.")

    return M, ss_args


def _steadystate_iterative(L, ss_args):
    """
    Iterative steady state solver using the GMRES, LGMRES, or BICGSTAB
    algorithm and a sparse incomplete LU preconditioner.
    """
    ss_iters = {'iter': 0}

    def _iter_count(r):
        ss_iters['iter'] += 1
        return

    if settings.debug:
        logger.debug('Starting %s solver.' % ss_args['method'])

    dims = L.dims[0]
    n = prod(L.dims[0][0])
    b = np.zeros(n ** 2)
    b[0] = ss_args['weight']

    L, perm, perm2, rev_perm, ss_args = _steadystate_LU_liouvillian(L, ss_args)
    if np.any(perm):
        b = b[np.ix_(perm,)]
    if np.any(perm2):
        b = b[np.ix_(perm2,)]

    use_solver(assumeSortedIndices=True, useUmfpack=ss_args['use_umfpack'])

    if ss_args['M'] is None and ss_args['use_precond']:
        ss_args['M'], ss_args = _iterative_precondition(L, n, ss_args)
        if ss_args['M'] is None:
            warnings.warn("Preconditioning failed. Continuing without.",
                          UserWarning)

    # Select iterative solver type
    _iter_start = time.time()
    if ss_args['method'] == 'iterative-gmres':
        v, check = gmres(L, b, tol=ss_args['tol'], M=ss_args['M'],
                            x0=ss_args['x0'], restart=ss_args['restart'],
                            maxiter=ss_args['maxiter'], callback=_iter_count)

    elif ss_args['method'] == 'iterative-lgmres':
        v, check = lgmres(L, b, tol=ss_args['tol'], M=ss_args['M'],
                          x0=ss_args['x0'], maxiter=ss_args['maxiter'],
                          callback=_iter_count)

    elif ss_args['method'] == 'iterative-bicgstab':
        v, check = bicgstab(L, b, tol=ss_args['tol'], M=ss_args['M'],
                            x0=ss_args['x0'],
                            maxiter=ss_args['maxiter'], callback=_iter_count)
    else:
        raise Exception("Invalid iterative solver method.")
    _iter_end = time.time()

    ss_args['info']['iter_time'] = _iter_end - _iter_start
    if 'precond_time' in ss_args['info'].keys():
        ss_args['info']['solution_time'] = (ss_args['info']['iter_time'] +
                                            ss_args['info']['precond_time'])
    ss_args['info']['iterations'] = ss_iters['iter']
    if ss_args['return_info']:
        ss_args['info']['residual_norm'] = la.norm(b - L*v, np.inf)

    if settings.debug:
        logger.debug('Number of Iterations: %i' % ss_iters['iter'])
        logger.debug('Iteration. time: %f' %  (_iter_end - _iter_start))

    if check > 0:
        raise Exception("Steadystate error: Did not reach tolerance after " +
                        str(ss_args['maxiter']) + " steps." +
                        "\nResidual norm: " +
                        str(ss_args['info']['residual_norm']))

    elif check < 0:
        raise Exception(
            "Steadystate error: Failed with fatal error: " + str(check) + ".")

    if ss_args['use_rcm']:
        v = v[np.ix_(rev_perm,)]

    data = vec2mat(v)
    data = 0.5 * (data + data.conj().T)
    if ss_args['return_info']:
        return Qobj(data, dims=dims, isherm=True), ss_args['info']
    else:
        return Qobj(data, dims=dims, isherm=True)


def _steadystate_svd_dense(L, ss_args):
    """
    Find the steady state(s) of an open quantum system by solving for the
    nullspace of the Liouvillian.
    """
    ss_args['info'].pop('weight', None)
    atol = 1e-12
    rtol = 1e-12
    if settings.debug:
        logger.debug('Starting SVD solver.')
    _svd_start = time.time()
    u, s, vh = svd(L.full(), full_matrices=False)
    tol = max(atol, rtol * s[0])
    nnz = (s >= tol).sum()
    ns = vh[nnz:].conj().T
    _svd_end = time.time()
    ss_args['info']['total_time'] = _svd_end-_svd_start
    if ss_args['all_states']:
        rhoss_list = []
        for n in range(ns.shape[1]):
            rhoss = Qobj(vec2mat(ns[:, n]), dims=L.dims[0])
            rhoss_list.append(rhoss / rhoss.tr())
        if ss_args['return_info']:
            return rhoss_list, ss_args['info']
        else:
            if ss_args['return_info']:
                return rhoss_list, ss_args['info']
            else:
                return rhoss_list
    else:
        rhoss = Qobj(vec2mat(ns[:, 0]), dims=L.dims[0])
        return rhoss / rhoss.tr()


def _steadystate_power(L, ss_args):
    """
    Inverse power method for steady state solving.
    """
    ss_args['info'].pop('weight', None)
    if settings.debug:
        logger.debug('Starting iterative inverse-power method solver.')
    tol = ss_args['tol']
    maxiter = ss_args['maxiter']

    use_solver(assumeSortedIndices=True)
    rhoss = Qobj()
    sflag = issuper(L)
    if sflag:
        rhoss.dims = L.dims[0]
    else:
        rhoss.dims = [L.dims[0], 1]
    n = prod(rhoss.shape)
    L = L.data.tocsc() - (1e-15) * sp.eye(n, n, format='csc')
    L.sort_indices()
    orig_nnz = L.nnz

    # start with all ones as RHS
    v = np.ones(n, dtype=complex)

    if ss_args['use_rcm']:
        if settings.debug:
            old_band = sp_bandwidth(L)[0]
            logger.debug('Original bandwidth: %i' % old_band)
        perm = reverse_cuthill_mckee(L)
        rev_perm = np.argsort(perm)
        L = sp_permute(L, perm, perm, 'csc')
        v = v[np.ix_(perm,)]
        if settings.debug:
            new_band = sp_bandwidth(L)[0]
            logger.debug('RCM bandwidth: %i' % new_band)
            logger.debug('Bandwidth reduction factor: %f' 
                            % round(old_band/new_band, 2))

    _power_start = time.time()
    # Get LU factors
    lu = splu(L, permc_spec=ss_args['permc_spec'],
              diag_pivot_thresh=ss_args['diag_pivot_thresh'],
              options=dict(ILU_MILU=ss_args['ILU_MILU']))

    if settings.debug and _scipy_check:
        L_nnz = lu.L.nnz
        U_nnz = lu.U.nnz
        logger.debug('L NNZ: %i ; U NNZ: %i' % (L_nnz,U_nnz))
        logger.debug('Fill factor: %f' % ((L_nnz+U_nnz)/orig_nnz))

    it = 0
    while (la.norm(L * v, np.inf) > tol) and (it < maxiter):
        v = lu.solve(v)
        v = v / la.norm(v, np.inf)
        it += 1
    if it >= maxiter:
        raise Exception('Failed to find steady state after ' +
                        str(maxiter) + ' iterations')

    _power_end = time.time()
    ss_args['info']['solution_time'] = _power_end-_power_start
    ss_args['info']['iterations'] = it
    if ss_args['return_info']:
        ss_args['info']['residual_norm'] = la.norm(L*v, np.inf)
    if settings.debug:
        logger.debug('Number of iterations: %i' % it)

    if ss_args['use_rcm']:
        v = v[np.ix_(rev_perm,)]

    # normalise according to type of problem
    if sflag:
        trow = sp.eye(rhoss.shape[0], rhoss.shape[0], format='coo')
        trow = sp_reshape(trow, (1, n))
        data = v / sum(trow.dot(v))
    else:
        data = data / la.norm(v)

    data = sp.csr_matrix(vec2mat(data))
    rhoss.data = 0.5 * (data + data.conj().T)
    rhoss.isherm = True
    if ss_args['return_info']:
        return rhoss, ss_args['info']
    else:
        return rhoss


def build_preconditioner(A, c_op_list=[], **kwargs):
    """Constructs a iLU preconditioner necessary for solving for
    the steady state density matrix using the iterative linear solvers
    in the 'steadystate' function.

    Parameters
    ----------
    A : qobj
        A Hamiltonian or Liouvillian operator.

    c_op_list : list
        A list of collapse operators.

    return_info : bool, optional, default = False
        Return a dictionary of solver-specific infomation about the
        solution and how it was obtained.

    use_rcm : bool, optional, default = False
        Use reverse Cuthill-Mckee reordering to minimize fill-in in the
        LU factorization of the Liouvillian.

    use_wbm : bool, optional, default = False
        Use Weighted Bipartite Matching reordering to make the Liouvillian
        diagonally dominant.  This is useful for iterative preconditioners
        only, and is set to ``True`` by default when finding a preconditioner.

    weight : float, optional
        Sets the size of the elements used for adding the unity trace condition
        to the linear solvers.  This is set to the average abs value of the
        Liouvillian elements if not specified by the user.

    permc_spec : str, optional, default='COLAMD'
        Column ordering used internally by superLU for the
        'direct' LU decomposition method. Options include 'COLAMD' and
        'NATURAL'. If using RCM then this is set to 'NATURAL' automatically
        unless explicitly specified.

    fill_factor : float, optional, default = 100
        Specifies the fill ratio upper bound (>=1) of the iLU
        preconditioner.  Lower values save memory at the cost of longer
        execution times and a possible singular factorization.

    drop_tol : float, optional, default = 1e-4
        Sets the threshold for the magnitude of preconditioner
        elements that should be dropped.  Can be reduced for a courser
        factorization at the cost of an increased number of iterations, and a
        possible singular factorization.

    diag_pivot_thresh : float, optional, default = None
        Sets the threshold between [0,1] for which diagonal
        elements are considered acceptable pivot points when using a
        preconditioner.  A value of zero forces the pivot to be the diagonal
        element.

    ILU_MILU : str, optional, default = 'smilu_2'
        Selects the incomplete LU decomposition method algoithm used in
        creating the preconditoner. Should only be used by advanced users.

    Returns
    -------
    lu : object
        Returns a SuperLU object representing iLU preconditioner.

    info : dict, optional
        Dictionary containing solver-specific information.
    """
    ss_args = _default_steadystate_args()
    for key in kwargs.keys():
        if key in ss_args.keys():
            ss_args[key] = kwargs[key]
        else:
            raise Exception("Invalid keyword argument '" + key +
                            "' passed to steadystate.")

    # Set column perm to NATURAL if using RCM and not specified by user
    if ss_args['use_rcm'] and ('permc_spec' not in kwargs.keys()):
        ss_args['permc_spec'] = 'NATURAL'

    L = _steadystate_setup(A, c_op_list)
    # Set weight parameter to avg abs val in L if not set explicitly
    if 'weight' not in kwargs.keys():
        ss_args['weight'] = np.mean(np.abs(L.data.data.max()))
        ss_args['info']['weight'] = ss_args['weight']

    n = prod(L.dims[0][0])
    L, perm, perm2, rev_perm, ss_args = _steadystate_LU_liouvillian(L, ss_args)
    M, ss_args = _iterative_precondition(L, n, ss_args)

    if ss_args['return_info']:
        return M, ss_args['info']
    else:
        return M
