import jax
import jax.numpy as np
import jax.flatten_util
import numpy as onp
from jax.experimental.sparse import BCOO
import scipy
import time
from petsc4py import PETSc
import gc

from jax_fem import logger

from jax import config
config.update("jax_enable_x64", True)

################################################################################
# JAX solver or UMFPACK solver or PETSc solver

def jax_solve(problem, A_fn, b, x0, precond):
    """Solves the equilibrium equation using a JAX solver.
    Is fully traceable and runs on GPU.

    Parameters
    ----------
    precond
        Whether to calculate the preconditioner or not
    """
    logger.debug(f"JAX Solver - Solving linear system")
    pc = get_jacobi_precond(jacobi_preconditioner(problem)) if precond else None
    x, info = jax.scipy.sparse.linalg.bicgstab(A_fn,
                                               b,
                                               x0=x0,
                                               M=pc,
                                               tol=1e-10,
                                               atol=1e-10,
                                               maxiter=10000)

    # Verify convergence
    err = np.linalg.norm(A_fn(x) - b)
    logger.debug(f"JAX Solver - Finshed solving, res = {err}")
    # if hasattr(problem, 'X_0'):
    #     if err > 0.1:
    #         return None
    assert err < 0.1, f"JAX linear solver failed to converge with err = {err}" # 0.1
    x = np.where(err < 0.1, x, np.nan) # For assert purpose, some how this also affects bicgstab.

    return x

def umfpack_solve(A, b):
    logger.debug(f"Scipy Solver - Solving linear system with UMFPACK")
    ai, aj, av = A.getValuesCSR()
    Asp = scipy.sparse.csr_matrix((av, aj, ai))
    x = scipy.sparse.linalg.spsolve(Asp, onp.array(b))

    # TODO: try https://jax.readthedocs.io/en/latest/_autosummary/jax.experimental.sparse.linalg.spsolve.html
    # x = jax.experimental.sparse.linalg.spsolve(av, aj, ai, b)

    logger.debug(f'Scipy Solver - Finished solving, linear solve res = {np.linalg.norm(Asp @ x - b)}')
    return x


def petsc_solve(A, b, ksp_type, pc_type):
    rhs = PETSc.Vec().createSeq(len(b))
    rhs.setValues(range(len(b)), onp.array(b))
    ksp = PETSc.KSP().create()
    ksp.setOperators(A)
    ksp.setFromOptions()
    ksp.setType(ksp_type)
    ksp.pc.setType(pc_type)

    # TODO: This works better. Do we need to generalize the code a little bit?
    if ksp_type == 'tfqmr':
        ksp.pc.setFactorSolverType('mumps')

    logger.debug(f'PETSc Solver - Solving linear system with ksp_type = {ksp.getType()}, pc = {ksp.pc.getType()}')
    x = PETSc.Vec().createSeq(len(b))
    ksp.solve(rhs, x)

    # Verify convergence
    y = PETSc.Vec().createSeq(len(b))
    A.mult(x, y)

    err = np.linalg.norm(y.getArray() - rhs.getArray())
    logger.debug(f"PETSc Solver - Finished solving, linear solve res = {err}")
    assert err < 0.1, f"PETSc linear solver failed to converge, err = {err}"

    # Delete PETSc objects
    ksp.destroy()
    rhs.destroy()
    y.destroy()
    A.destroy()
    result = x.getArray()
    x.destroy()
    gc.collect()
    return result


################################################################################
# "row elimination" solver

def apply_bc_vec(res_vec, dofs, problem):
    res_list = problem.unflatten_fn_sol_list(res_vec)
    sol_list = problem.unflatten_fn_sol_list(dofs)

    for ind, fe in enumerate(problem.fes):
        res = res_list[ind]
        sol = sol_list[ind]
        for i in range(len(fe.node_inds_list)):
            res = (res.at[fe.node_inds_list[i], fe.vec_inds_list[i]].set(
                sol[fe.node_inds_list[i], fe.vec_inds_list[i]], unique_indices=True))
            res = res.at[fe.node_inds_list[i], fe.vec_inds_list[i]].add(-fe.vals_list[i])

        res_list[ind] = res

    return jax.flatten_util.ravel_pytree(res_list)[0]


def apply_bc(res_fn, problem):

    def A_fn(dofs):
        """Apply Dirichlet boundary conditions
        """
        res_vec = res_fn(dofs)
        return apply_bc_vec(res_vec, dofs, problem)

    return A_fn


def row_elimination(fn, problem):

    def fn_dofs_row(dofs):
        res_vec = fn(dofs)
        res_list = problem.unflatten_fn_sol_list(res_vec)
        sol_list = problem.unflatten_fn_sol_list(dofs)
        for ind, fe in enumerate(problem.fes):
            res = res_list[ind]
            sol = sol_list[ind]
            for i in range(len(fe.node_inds_list)):
                res = (res.at[fe.node_inds_list[i], fe.vec_inds_list[i]].set(
                       sol[fe.node_inds_list[i], fe.vec_inds_list[i]], unique_indices=True))
            res_list[ind] = res

        return jax.flatten_util.ravel_pytree(res_list)[0]

    return fn_dofs_row


def assign_bc(dofs, problem):
    sol_list = problem.unflatten_fn_sol_list(dofs)
    for ind, fe in enumerate(problem.fes):
        sol = sol_list[ind]
        for i in range(len(fe.node_inds_list)):
            sol = sol.at[fe.node_inds_list[i],
                         fe.vec_inds_list[i]].set(fe.vals_list[i])
        sol_list[ind] = sol
    return jax.flatten_util.ravel_pytree(sol_list)[0]


def assign_ones_bc(dofs, problem):
    sol_list = problem.unflatten_fn_sol_list(dofs)
    for ind, fe in enumerate(problem.fes):
        sol = sol_list[ind]
        for i in range(len(fe.node_inds_list)):
            sol = sol.at[fe.node_inds_list[i],
                         fe.vec_inds_list[i]].set(1.)
        sol_list[ind] = sol
    return jax.flatten_util.ravel_pytree(sol_list)[0]


def assign_zeros_bc(dofs, problem):
    sol_list = problem.unflatten_fn_sol_list(dofs)
    for ind, fe in enumerate(problem.fes):
        sol = sol_list[ind]
        for i in range(len(fe.node_inds_list)):
            sol = sol.at[fe.node_inds_list[i],
                         fe.vec_inds_list[i]].set(0.)
        sol_list[ind] = sol
    return jax.flatten_util.ravel_pytree(sol_list)[0]


def copy_bc(dofs, problem):
    new_dofs = np.zeros_like(dofs)
    sol_list = problem.unflatten_fn_sol_list(dofs)
    new_sol_list = problem.unflatten_fn_sol_list(new_dofs)
  
    for ind, fe in enumerate(problem.fes):
        sol = sol_list[ind]
        new_sol = new_sol_list[ind]
        for i in range(len(fe.node_inds_list)):
            new_sol = (new_sol.at[fe.node_inds_list[i],
                                  fe.vec_inds_list[i]].set(sol[fe.node_inds_list[i],
                                          fe.vec_inds_list[i]]))
        new_sol_list[ind] = new_sol

    return jax.flatten_util.ravel_pytree(new_sol_list)[0]


def get_flatten_fn(fn_sol_list, problem):

    def fn_dofs(dofs):
        sol_list = problem.unflatten_fn_sol_list(dofs)
        val_list = fn_sol_list(sol_list)
        return jax.flatten_util.ravel_pytree(val_list)[0]

    return fn_dofs


def get_A_fn_linear_fn(dofs, fn):
    """Not quite used.
    """
    def A_fn_linear_fn(inc):
        primals, tangents = jax.jvp(fn, (dofs, ), (inc, ))
        return tangents

    return A_fn_linear_fn


def get_A_fn_linear_fn_JFNK(dofs, fn):
    """Jacobian-free Newton–Krylov (JFNK) method.
    Not quite used since we have auto diff to compute exact JVP.
    Knoll, Dana A., and David E. Keyes.
    "Jacobian-free Newton–Krylov methods: a survey of approaches and applications."
    Journal of Computational Physics 193.2 (2004): 357-397.
    """
    def A_fn_linear_fn(inc):
        EPS = 1e-3
        return (fn(dofs + EPS * inc) - fn(dofs)) / EPS

    return A_fn_linear_fn


def operator_to_matrix(operator_fn, problem):
    """Only used for when debugging.
    Can be used to print the matrix, check the conditional number, etc.
    """
    J = jax.jacfwd(operator_fn)(np.zeros(problem.num_total_dofs_all_vars))
    return J


def jacobi_preconditioner(problem):
    jacobi = np.array(problem.A_sp_scipy.diagonal())
    jacobi = assign_ones_bc(jacobi.reshape(-1), problem)
    return jacobi


def get_jacobi_precond(jacobi):

    def jacobi_precond(x):
        return x * (1. / jacobi)

    return jacobi_precond


def test_jacobi_precond(problem, jacobi, A_fn):
    """Not working, needs refactoring
    """
    num_total_dofs = problem.num_total_nodes * problem.vec
    for ind in range(500):
        test_vec = np.zeros(num_total_dofs)
        test_vec = test_vec.at[ind].set(1.)
        logger.debug(f"{A_fn(test_vec)[ind]}, {jacobi[ind]}, ratio = {A_fn(test_vec)[ind]/jacobi[ind]}")

    logger.debug(f"test jacobi preconditioner")
    logger.debug(f"np.min(jacobi) = {np.min(jacobi)}, np.max(jacobi) = {np.max(jacobi)}")
    logger.debug(f"finish jacobi preconditioner")


def linear_incremental_solver(problem, res_vec, A_fn, dofs, solver_options):
    """
    Linear solver at each Newton's iteration
    """
    logger.debug(f"Solving linear system...")
    b = -res_vec

    # If user does not specify any solver, set jax_solver as the default one.
    if  len(solver_options.keys() & {'jax_solver', 'umfpack_solver', 'petsc_solver'}) == 0: 
        solver_options['jax_solver'] = {}

    if 'jax_solver' in solver_options:
        # x0 will always be correct at boundary locations
        x0_1 = assign_bc(np.zeros_like(b), problem)
        x0_2 = copy_bc(dofs, problem)
        x0 = x0_1 - x0_2
        precond = solver_options['jax_solver']['precond'] if 'precond' in solver_options['jax_solver'] else True
        inc = jax_solve(problem, A_fn, b, x0, precond)
        # if hasattr(problem, 'X_0'):
        #     if inc == None:
        #         return None
        
    elif 'umfpack_solver' in solver_options:
        inc = umfpack_solve(A_fn, b)
    else:
        ksp_type = solver_options['petsc_solver']['ksp_type'] if 'ksp_type' in solver_options['petsc_solver'] else  'bcgsl' 
        pc_type = solver_options['petsc_solver']['pc_type'] if 'pc_type' in solver_options['petsc_solver'] else 'ilu'
        inc = petsc_solve(A_fn, b, ksp_type, pc_type)

    line_search_flag = solver_options['line_search_flag'] if 'line_search_flag' in solver_options else False
    if line_search_flag:
        dofs = line_search(problem, dofs, inc)
    else:
        dofs = dofs + inc

    return dofs


def line_search(problem, dofs, inc):
    """
    TODO: This is useful for finite deformation plasticity.
    """
    res_fn = problem.compute_residual
    res_fn = get_flatten_fn(res_fn, problem)
    res_fn = apply_bc(res_fn, problem)

    def res_norm_fn(alpha):
        res_vec = res_fn(dofs + alpha*inc)
        return np.linalg.norm(res_vec)

    # grad_res_norm_fn = jax.grad(res_norm_fn)
    # hess_res_norm_fn = jax.hessian(res_norm_fn)

    # tol = 1e-3
    # alpha = 1.
    # lr = 1.
    # grad_alpha = 1.
    # while np.abs(grad_alpha) > tol:
    #     grad_alpha = grad_res_norm_fn(alpha)
    #     hess_alpha = hess_res_norm_fn(alpha)
    #     alpha = alpha - 1./hess_alpha*grad_alpha
    #     print(f"alpha = {alpha}, grad_alpha = {grad_alpha}, hess_alpha = {hess_alpha}")

    alpha = 1.
    res_norm = res_norm_fn(alpha)
    for i in range(3):
        alpha *= 0.5
        res_norm_half = res_norm_fn(alpha)
        print(f"i = {i}, res_norm = {res_norm}, res_norm_half = {res_norm_half}")
        if res_norm_half > res_norm:
            alpha *= 2.
            break
        res_norm = res_norm_half

    return dofs + alpha*inc


def get_A_fn(problem, solver_options):
    logger.debug(f"Creating sparse matrix with scipy...")
    A_sp_scipy = scipy.sparse.csr_array(
        # (onp.array(problem.V), (problem.I, problem.J)),
        (onp.array(problem.V), (problem.I, problem.J)),
        shape=(problem.num_total_dofs_all_vars, problem.num_total_dofs_all_vars))
    # logger.debug(f"Creating sparse matrix from scipy using JAX BCOO...")
    A_sp = BCOO.from_scipy_sparse(A_sp_scipy).sort_indices()
    # logger.info(f"Global sparse matrix takes about {A_sp.data.shape[0]*8*3/2**30} G memory to store.")
    problem.A_sp_scipy = A_sp_scipy

    def compute_linearized_residual(dofs):
        return A_sp @ dofs

    jax_type = True if len(solver_options.keys() & {'umfpack_solver', 'petsc_solver'}) == 0 else False 

    if jax_type:
        A = row_elimination(compute_linearized_residual, problem)

  
    else:
        # https://scicomp.stackexchange.com/questions/2355/32bit-64bit-issue-when-working-with-numpy-and-petsc4py/2356#2356
        A = PETSc.Mat().createAIJ(size=A_sp_scipy.shape, csr=(A_sp_scipy.indptr.astype(PETSc.IntType, copy=False),
                                                       A_sp_scipy.indices.astype(PETSc.IntType, copy=False), A_sp_scipy.data))

        for ind, fe in enumerate(problem.fes):
            for i in range(len(fe.node_inds_list)):
                row_inds = onp.array(fe.node_inds_list[i] * fe.vec + fe.vec_inds_list[i] + problem.offset[ind], dtype=onp.int32)
                A.zeroRows(row_inds)

    return A


################################################################################
# The "row elimination" solver

def solver(problem, solver_options={}):
    """
    Specify exactly either 'jax_solver' or 'umfpack_solver' or 'petsc_solver'
    
    Examples:
    (1) solver_options = {'jax_solver': {}}
    (2) solver_options = {'umfpack_solver': {}}
    (3) solver_options = {'petsc_solver': {'ksp_type': 'bcgsl', 'pc_type': 'jacobi'}, 'initial_guess': some_guess}

    Default parameters will be used if no instruction is found:

    solver_options = 
    {
        # If multiple solvers are specified or no solver is specified, 'jax_solver' will be used.
        'jax_solver': 
        {
            # The JAX built-in linear solver 
            # Reference: https://jax.readthedocs.io/en/latest/_autosummary/jax.scipy.sparse.linalg.bicgstab.html
            'precond': True,
        }

        'umfpack_solver': 
        {   
            # The scipy solver that calls UMFPACK
            # Reference: https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.linalg.spsolve.html
        }

        'petsc_solver':
        {   
            # PETSc solver
            # For more ksp_type and pc_type: https://www.mcs.anl.gov/petsc/petsc4py-current/docs/apiref/index.html
            'ksp_type': 'bcgsl', # e.g., 'minres', 'gmres', 'tfqmr'
            'pc_type': 'ilu', # e.g., 'jacobi'
        }

        'line_search_flag': False, # Line search method
        'initial_guess': initial_guess, # Same shape as sol_list
        'tol': 1e-5, # Absolute tolerance for residual vector (l2 norm), used in Newton's method
        'rel_tol': 1e-8, # Relative tolerance for residual vector (l2 norm), used in Newton's method
    }

    The solver imposes Dirichlet B.C. with "row elimination" method.

    Some memo:

    res(u) = D*r(u) + (I - D)u - u_b
    D = [[1 0 0 0]
         [0 1 0 0]
         [0 0 0 0]
         [0 0 0 1]]
    I = [[1 0 0 0]
         [0 1 0 0]
         [0 0 1 0]
         [0 0 0 1]
    A_fn = d(res)/d(u) = D*dr/du + (I - D)

    The function newton_update computes r(u) and dr/du
    """
    logger.debug(f"Calling the row elimination solver for imposing Dirichlet B.C.")
    logger.debug("Start timing")
    start = time.time()

    if 'initial_guess' in solver_options:
        # We dont't want inititual guess to play a role in the differentiation chain.
        initial_guess = jax.lax.stop_gradient(solver_options['initial_guess'])
        dofs = jax.flatten_util.ravel_pytree(initial_guess)[0]
    else:
        dofs = np.zeros(problem.num_total_dofs_all_vars)

    rel_tol = solver_options['rel_tol'] if 'rel_tol' in solver_options else 1e-8
    tol = solver_options['tol'] if 'tol' in solver_options else 1e-6

    def newton_update_helper(dofs):
        sol_list = problem.unflatten_fn_sol_list(dofs)
        if hasattr(problem, 'X_0'):
            res_list, Jaco_mean = problem.newton_update(sol_list)
        else:
            res_list= problem.newton_update(sol_list)
        # if res_list == None:
        #     return None, []
        res_vec = jax.flatten_util.ravel_pytree(res_list)[0]
        res_vec = apply_bc_vec(res_vec, dofs, problem)
        A_fn = get_A_fn(problem, solver_options)

        if hasattr(problem, 'X_0'):
            return res_vec, A_fn, Jaco_mean
        else:
            return res_vec, A_fn


    if hasattr(problem, 'X_0'):
        res_vec, A_fn, Jaco_mean = newton_update_helper(dofs)
    else:
        res_vec, A_fn = newton_update_helper(dofs)


    # if res_vec == None:
    #     return None
    res_val = np.linalg.norm(res_vec)
    res_val_initial = res_val
    rel_res_val = res_val/res_val_initial
    logger.debug(f"Before, l_2 res = {res_val}, relative l_2 res = {rel_res_val}")
    while (rel_res_val > rel_tol) and (res_val > tol):
        dofs = linear_incremental_solver(problem, res_vec, A_fn, dofs, solver_options)
        # if dofs == None:
        #     return None
        if hasattr(problem, 'X_0'):
            res_vec, A_fn, Jaco_mean = newton_update_helper(dofs)
        else:
            res_vec, A_fn = newton_update_helper(dofs)
        # if res_vec == None:
        #     return None
        # logger.debug(f"DEBUG: l_2 res = {np.linalg.norm(apply_bc_vec(A_fn(dofs), dofs, problem))}")
        # test_jacobi_precond(problem, jacobi_preconditioner(problem, dofs), A_fn)
        res_val = np.linalg.norm(res_vec)
        rel_res_val = res_val/res_val_initial
        logger.debug(f"l_2 res = {res_val}, relative l_2 res = {rel_res_val}")

    assert np.all(np.isfinite(res_val)), f"res_val contains NaN, stop the program!"
    assert np.all(np.isfinite(dofs)), f"dofs contains NaN, stop the program!"

    # If sol_list = [[[u1x, u1y], 
    #                 [u2x, u2y], 
    #                 [u3x, u3y], 
    #                 [u4x, u4y]], 
    #                [[p1], 
    #                 [p2]]],
    # the flattend DOF vector will be [u1x, u1y, u2x, u2y, u3x, u3y, u4x, u4y, p1, p2]
    sol_list = problem.unflatten_fn_sol_list(dofs)

    end = time.time()
    solve_time = end - start
    logger.info(f"Solve took {solve_time} [s]")
    logger.debug(f"max of dofs = {np.max(dofs)}")
    logger.debug(f"min of dofs = {np.min(dofs)}")


    if "petsc_solver" in solver_options:
        A_fn.destroy()
    else:
        del A_fn

    del dofs
    del res_vec
    gc.collect()

    if hasattr(problem, 'X_0'):
    # Safe, outside of JIT
        return sol_list, Jaco_mean
    else:
        return sol_list, np.array(0.0)


################################################################################
# Dynamic relaxation solver

def assembleCSR(problem, dofs):
    sol_list = problem.unflatten_fn_sol_list(dofs)
    problem.newton_update(sol_list)
   
    A_sp_scipy = scipy.sparse.csr_array((problem.V, (problem.I, problem.J)),
        shape=(problem.fes[0].num_total_dofs, problem.fes[0].num_total_dofs))
    A = PETSc.Mat().createAIJ(size=A_sp_scipy.shape, 
                              csr=(A_sp_scipy.indptr.astype(PETSc.IntType, copy=False),
                                   A_sp_scipy.indices.astype(PETSc.IntType, copy=False), 
                                   A_sp_scipy.data))

    for i in range(len(problem.fes[0].node_inds_list)):
        row_inds = onp.array(problem.fes[0].node_inds_list[i] * problem.fes[0].vec +
                             problem.fes[0].vec_inds_list[i],
                             dtype=onp.int32)
        A.zeroRows(row_inds)

    row, col, val = A.getValuesCSR()
    A_sp_scipy.data = val
    A_sp_scipy.indices = col
    A_sp_scipy.indptr = row

    return A_sp_scipy


def calC(t, cmin, cmax):

    if t < 0.: t = 0.

    c = 2. * onp.sqrt(t)
    if (c < cmin): c = cmin
    if (c > cmax): c = cmax

    return c


def printInfo(error, t, c, tol, eps, qdot, qdotdot, nIters, nPrint, info, info_force):

    ## printing control
    if nIters % nPrint == 1:
        #logger.info('\t------------------------------------')
        if info_force == True:
            print(('\nDR Iteration %d: Max force (residual error) = %g (tol = %g)' +
                   'Max velocity = %g') % (nIters, error, tol,
                                            np.max(np.absolute(qdot))))
        if info == True:
            print('\nDamping t: ',t, );
            print('Damping coefficient: ', c)
            print('Max epsilon: ',np.max(eps))
            print('Max acceleration: ',np.max(np.absolute(qdotdot)))


def dynamic_relax_solve(problem, tol=1e-6, nKMat=50, nPrint=500, info=True, info_force=True):
    """
    Implementation of

    Luet, David Joseph. Bounding volume hierarchy and non-uniform rational B-splines for contact enforcement
    in large deformation finite element analysis of sheet metal forming. Diss. Princeton University, 2016.
    Chapter 4.3 Nonlinear System Solution

    Particularly good for handling buckling behavior.
    There is a FEniCS version of this dynamic relaxation algorithm.
    The code below is a direct translation from the FEniCS version.
    """
    solver_options = {'umfpack_solver': {}}

    def newton_update_helper(dofs):
        sol_list = problem.unflatten_fn_sol_list(dofs)
        res_list = problem.newton_update(sol_list)
        res_vec = jax.flatten_util.ravel_pytree(res_list)[0]
        res_vec = apply_bc_vec(res_vec, dofs, problem)
        A_fn = get_A_fn(problem, solver_options)
        return res_vec, A_fn
 
    dofs = np.zeros(problem.num_total_dofs_all_vars)
    res_vec, A_fn = newton_update_helper(dofs)

    dofs = linear_incremental_solver(problem, res_vec, A_fn, dofs, solver_options)

    # parameters not to change
    cmin = 1e-3
    cmax = 3.9
    h_tilde = 1.1
    h = 1.

    # initialize all arrays
    N = len(dofs)  #print("--------num of DOF's: %d-----------" % N)
    #initialize displacements, velocities and accelerations
    q, qdot, qdotdot = onp.zeros(N), onp.zeros(N), onp.zeros(N)
    #initialize displacements, velocities and accelerations from a previous time step
    q_old, qdot_old, qdotdot_old = onp.zeros(N), onp.zeros(N), onp.zeros(N)
    #initialize the M, eps, R_old arrays
    eps, M, R, R_old = onp.zeros(N), onp.zeros(N), onp.zeros(N), onp.zeros(N)

    @jax.jit
    def assembleVec(dofs):
        res_fn = get_flatten_fn(problem.compute_residual, problem)
        res_vec = res_fn(dofs)
        res_vec = assign_zeros_bc(res_vec, problem)
        return res_vec

    R = onp.array(assembleVec(dofs))
    KCSR = assembleCSR(problem, dofs)

    M[:] = h_tilde * h_tilde / 4. * onp.array(
        onp.absolute(KCSR).sum(axis=1)).squeeze()
    q[:] = dofs
    qdot[:] = -h / 2. * R / M
    # set the counters for iterations and
    nIters, iKMat = 0, 0
    error = 1.0
    timeZ = time.time() #Measurement of loop time.

    assert onp.all(onp.isfinite(M)), f"M not finite"
    assert onp.all(onp.isfinite(q)), f"q not finite"
    assert onp.all(onp.isfinite(qdot)), f"qdot not finite"

    error = onp.max(onp.absolute(R))

    while error > tol:

        print(f"error = {error}")
        # marching forward
        q_old[:] = q[:]; R_old[:] = R[:]
        q[:] += h*qdot; dofs = np.array(q)

        R = onp.array(assembleVec(dofs))

        nIters += 1
        iKMat += 1
        error = onp.max(onp.absolute(R))

        # damping calculation
        S0 = onp.dot((R - R_old) / h, qdot)
        t = S0 / onp.einsum('i,i,i', qdot, M, qdot)
        c = calC(t, cmin, cmax)

        # determine whether to recal KMat
        eps = h_tilde * h_tilde / 4. * onp.absolute(
            onp.divide((qdotdot - qdotdot_old), (q - q_old),
                       out=onp.zeros_like((qdotdot - qdotdot_old)),
                       where=(q - q_old) != 0))

        # calculating the jacobian matrix
        if ((onp.max(eps) > 1) and (iKMat > nKMat)): #SPR JAN max --> min
            if info == True:
                print('\nRecalculating the tangent matrix: ', nIters)

            iKMat = 0
            KCSR = assembleCSR(problem, dofs)
            M[:] = h_tilde * h_tilde / 4. * onp.array(
                onp.absolute(KCSR).sum(axis=1)).squeeze()

        # compute new velocities and accelerations
        qdot_old[:] = qdot[:]; qdotdot_old[:] = qdotdot[:];
        qdot = (2.- c*h)/(2 + c*h) * qdot_old - 2.*h/(2.+c*h)* R / M
        qdot_old[:] = qdot[:]
        qdotdot = qdot - qdot_old

        # output on screen
        printInfo(error, t, c, tol, eps, qdot, qdotdot, nIters, nPrint, info, info_force)

    # check if converged
    convergence = True
    if onp.isnan(onp.max(onp.absolute(R))):
        convergence = False

    # print final info
    if convergence:
        print("DRSolve finished in %d iterations and %fs" % \
              (nIters, time.time() - timeZ))
    else:
        print("FAILED to converged")

    sol_list = problem.unflatten_fn_sol_list(dofs)

        
    return sol_list[0]


################################################################################
# Implicit differentiation with the adjoint method

def implicit_vjp(problem, sol_list, params, v_list, adjoint_solver_options):

    def constraint_fn(dofs, params):
        """c(u, p)
        """
        problem.set_params(params)
        res_fn = problem.compute_residual
        res_fn = get_flatten_fn(res_fn, problem)
        res_fn = apply_bc(res_fn, problem)
        return res_fn(dofs)

    def constraint_fn_sol_to_sol(sol_list, params):
        dofs = jax.flatten_util.ravel_pytree(sol_list)[0]
        con_vec = constraint_fn(dofs, params)
        return problem.unflatten_fn_sol_list(con_vec)

    def get_vjp_contraint_fn_dofs(dofs):
        # Just a transpose of A_fn
        def adjoint_linear_fn(adjoint):
            primals, f_vjp = jax.vjp(A_fn, dofs)
            val, = f_vjp(adjoint)
            return val

        return adjoint_linear_fn

    def get_partial_params_c_fn(sol_list):
        """c(u=u, p)
        """
        def partial_params_c_fn(params):
            return constraint_fn_sol_to_sol(sol_list, params)

        return partial_params_c_fn

    def get_vjp_contraint_fn_params(params, sol_list):
        """v*(partial dc/dp)
        """
        partial_c_fn = get_partial_params_c_fn(sol_list)

        def vjp_linear_fn(v_list):
            primals, f_vjp = jax.vjp(partial_c_fn, params)
            val, = f_vjp(v_list)
            return val

        return vjp_linear_fn

    problem.set_params(params)
    _, _ = problem.newton_update(sol_list)

    A_fn = get_A_fn(problem, adjoint_solver_options)
    v_vec = jax.flatten_util.ravel_pytree(v_list)[0]

    if adjoint_solver_options == {}:
        adjoint_solver_options = {'jax_solver': {}}

    # If user does not specify any solver, set jax_solver as the default one.
    if  len(adjoint_solver_options.keys() & {'jax_solver', 'umfpack_solver', 'petsc_solver'}) == 0: 
        adjoint_solver_options['jax_solver'] = {}

    if 'jax_solver' in adjoint_solver_options:
        dofs = jax.flatten_util.ravel_pytree(sol_list)[0]
        adjoint_linear_fn = get_vjp_contraint_fn_dofs(dofs)
        precond = adjoint_solver_options['jax_solver']['precond'] if 'precond' in adjoint_solver_options['jax_solver'] else True
        adjoint_vec = jax_solve(problem, adjoint_linear_fn, v_vec, None, precond)
    elif 'umfpack_solver' in adjoint_solver_options:
        A_transpose = A_fn.transpose()
        adjoint_vec = umfpack_solve(A_transpose, v_vec)
    else:
        A_transpose = A_fn.transpose()

        # Remark: Eliminating rows seems to make A better conditioned.
        # If Dirichlet B.C. is part of the design variable, the following should NOT be implemented.
        # for i in range(len(problem.node_inds_list)):
        #     row_inds = onp.array(problem.node_inds_list[i]*problem.vec + problem.vec_inds_list[i], dtype=onp.int32)
        #     A_transpose.zeroRows(row_inds)
        # v_vec = assign_zeros_bc(v_vec, problem)

        ksp_type = adjoint_solver_options['petsc_solver']['ksp_type'] if 'ksp_type' in adjoint_solver_options['petsc_solver'] else  'bcgsl' 
        pc_type = adjoint_solver_options['petsc_solver']['pc_type'] if 'pc_type' in adjoint_solver_options['petsc_solver'] else 'ilu'
        adjoint_vec = petsc_solve(A_transpose, v_vec, ksp_type, pc_type)
 
    vjp_linear_fn = get_vjp_contraint_fn_params(params, sol_list)
    vjp_result = vjp_linear_fn(problem.unflatten_fn_sol_list(adjoint_vec))
    vjp_result = jax.tree_map(lambda x: -x, vjp_result)

    return vjp_result



def ad_wrapper(problem, solver_options={}, adjoint_solver_options={}):
    @jax.custom_vjp
    def fwd_pred(params):
        problem.set_params(params)
        sol_list, Jaco_mean = solver(problem, solver_options)
        # return sol_list, Jaco_mean
        return sol_list, Jaco_mean


    def f_fwd(params):
        sol_list, Jaco_mean = fwd_pred(params)
        return (sol_list, Jaco_mean), (params, sol_list, Jaco_mean)

    def f_bwd(res, v):
        logger.info("Running backward and solving the adjoint problem...")
        # params, sol_list, Jaco_mean = res
        params, sol_list, _ = res  # ignore Jaco_mean

        if isinstance(v, tuple):
            v = v[0]  # Use only the part corresponding to sol_list

        vjp_result = implicit_vjp(problem, sol_list, params, v, adjoint_solver_options)
        return (vjp_result, )

    fwd_pred.defvjp(f_fwd, f_bwd)
    return fwd_pred




# def ad_wrapper(problem, solver_options={}, adjoint_solver_options={}):
    # def penalty_only_fn(p):
    #     problem.set_params(p)
    #     # solver returns (sol_list, penalty)
    #     _, penalty = solver(problem, solver_options)
    #     return penalty

    # @jax.custom_vjp
    # def fwd_pred(params):
    #     problem.set_params(params)
    #     sol_list, penalty = solver(problem, solver_options)

    #     pen_grad = jax.grad(penalty_only_fn)(params)
    #     # return sol_list, Jaco_mean
    #     return (sol_list, penalty), (params, sol_list, penalty, pen_grad)


    # def f_fwd(params):
    #     (sol_list, penalty), residuals = fwd_pred(params)
    #     return (sol_list, penalty), residuals

    # def f_bwd(residuals, v):
    #     logger.info("Running backward and solving the adjoint problem...")
    #     # Unpack residuals: we now have the stored penalty gradient.
    #     params, sol_list, penalty, pen_grad = residuals
    #     # Unpack the incoming tangent (adjoint) for (sol_list, penalty)
    #     if not isinstance(v, tuple):
    #         v = (v, 0.0)
    #     v_sol, v_pen = v

    #     # Compute the gradient contribution from the solution output.
    #     sol_grad = implicit_vjp(problem, sol_list, params, v_sol, adjoint_solver_options)
    #     # Use the stored penalty sensitivity to scale by v_pen.
    #     total_grad = sol_grad + pen_grad * v_pen
    #     return (total_grad,)

    # fwd_pred.defvjp(f_fwd, f_bwd)
    # return fwd_pred



















    # def ad_wrapper(problem, solver_options={}, adjoint_solver_options={}):
    # @jax.custom_vjp
    # def fwd_pred(params):
    #     problem.set_params(params)
    #     sol_list, Jaco_mean = solver(problem, solver_options)
    #     # return sol_list, Jaco_mean
    #     return sol_list, Jaco_mean


    # def f_fwd(params):
    #     sol_list, Jaco_mean = fwd_pred(params)
    #     return (sol_list, Jaco_mean), (params, sol_list, Jaco_mean)

    # def f_bwd(res, v):
    #     logger.info("Running backward and solving the adjoint problem...")
    #     # params, sol_list, Jaco_mean = res
    #     params, sol_list, _ = res  # ignore Jaco_mean

    #     if isinstance(v, tuple):
    #         v = v[0]  # Use only the part corresponding to sol_list

    #     vjp_result = implicit_vjp(problem, sol_list, params, v, adjoint_solver_options)
    #     return (vjp_result, )

    # fwd_pred.defvjp(f_fwd, f_bwd)
    # return fwd_pred