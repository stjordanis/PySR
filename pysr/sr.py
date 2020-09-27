import os
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
from collections import namedtuple
import pathlib
import numpy as np
import pandas as pd

def pysr(X=None, y=None, weights=None,
            procs=4,
            populations=None,
            niterations=100,
            ncyclesperiteration=300,
            binary_operators=["plus", "mult"],
            unary_operators=["cos", "exp", "sin"],
            alpha=0.1,
            annealing=True,
            fractionReplaced=0.10,
            fractionReplacedHof=0.10,
            npop=1000,
            parsimony=1e-4,
            migration=True,
            hofMigration=True,
            shouldOptimizeConstants=True,
            topn=10,
            weightAddNode=1,
            weightInsertNode=3,
            weightDeleteNode=3,
            weightDoNothing=1,
            weightMutateConstant=10,
            weightMutateOperator=1,
            weightRandomize=1,
            weightSimplify=0.01,
            perturbationFactor=1.0,
            nrestarts=3,
            timeout=None,
            equation_file='hall_of_fame.csv',
            test='simple1',
            verbosity=1e9,
            maxsize=20,
            slurm_cluster=False,
            cluster_nodes=4,
            threads=None, #deprecated
            julia_optimization=3,
        ):
    """Run symbolic regression to fit f(X[i, :]) ~ y[i] for all i.
    Note: most default parameters have been tuned over several example
    equations, but you should adjust `threads`, `niterations`,
    `binary_operators`, `unary_operators` to your requirements.

    :param X: np.ndarray, 2D array. Rows are examples, columns are features.
    :param y: np.ndarray, 1D array. Rows are examples.
    :param weights: np.ndarray, 1D array. Each row is how to weight the
        mean-square-error loss on weights.
    :param procs: int, Number of processes (=number of populations running).
    :param populations: int, Number of populations running; by default=procs.
    :param niterations: int, Number of iterations of the algorithm to run. The best
        equations are printed, and migrate between populations, at the
        end of each.
    :param ncyclesperiteration: int, Number of total mutations to run, per 10
        samples of the population, per iteration.
    :param binary_operators: list, List of strings giving the binary operators
        in Julia's Base, or in `operator.jl`.
    :param unary_operators: list, Same but for operators taking a single `Float32`.
    :param alpha: float, Initial temperature.
    :param annealing: bool, Whether to use annealing. You should (and it is default).
    :param fractionReplaced: float, How much of population to replace with migrating
        equations from other populations.
    :param fractionReplacedHof: float, How much of population to replace with migrating
        equations from hall of fame.
    :param npop: int, Number of individuals in each population
    :param parsimony: float, Multiplicative factor for how much to punish complexity.
    :param migration: bool, Whether to migrate.
    :param hofMigration: bool, Whether to have the hall of fame migrate.
    :param shouldOptimizeConstants: bool, Whether to numerically optimize
        constants (Nelder-Mead/Newton) at the end of each iteration.
    :param topn: int, How many top individuals migrate from each population.
    :param nrestarts: int, Number of times to restart the constant optimizer
    :param perturbationFactor: float, Constants are perturbed by a max
        factor of (perturbationFactor*T + 1). Either multiplied by this
        or divided by this.
    :param weightAddNode: float, Relative likelihood for mutation to add a node
    :param weightInsertNode: float, Relative likelihood for mutation to insert a node
    :param weightDeleteNode: float, Relative likelihood for mutation to delete a node
    :param weightDoNothing: float, Relative likelihood for mutation to leave the individual
    :param weightMutateConstant: float, Relative likelihood for mutation to change
        the constant slightly in a random direction.
    :param weightMutateOperator: float, Relative likelihood for mutation to swap
        an operator.
    :param weightRandomize: float, Relative likelihood for mutation to completely
        delete and then randomly generate the equation
    :param weightSimplify: float, Relative likelihood for mutation to simplify
        constant parts by evaluation
    :param timeout: float, Time in seconds to timeout search
    :param equation_file: str, Where to save the files (.csv separated by |)
    :param test: str, What test to run, if X,y not passed.
    :param maxsize: int, Max size of an equation.
    :param julia_optimization: int, Optimization level (0, 1, 2, 3)
    :returns: pd.DataFrame, Results dataframe, giving complexity, MSE, and equations
        (as strings).

    """
    if threads is not None:
        raise ValueError("The threads kwarg is deprecated. Use procs.")

    # Check for potential errors before they happen
    assert len(binary_operators) > 0
    assert len(unary_operators) > 0
    assert len(X.shape) == 2
    assert len(y.shape) == 1
    assert X.shape[0] == y.shape[0]
    if weights is not None:
        assert len(weights.shape) == 1
        assert X.shape[0] == weights.shape[0]

    if populations is None:
        populations = procs

    rand_string = f'{"".join([str(np.random.rand())[2] for i in range(20)])}'

    if isinstance(binary_operators, str): binary_operators = [binary_operators]
    if isinstance(unary_operators, str): unary_operators = [unary_operators]

    if X is None:
        if test == 'simple1':
            eval_str = "np.sign(X[:, 2])*np.abs(X[:, 2])**2.5 + 5*np.cos(X[:, 3]) - 5"
        elif test == 'simple2':
            eval_str = "np.sign(X[:, 2])*np.abs(X[:, 2])**3.5 + 1/(np.abs(X[:, 0])+1)"
        elif test == 'simple3':
            eval_str = "np.exp(X[:, 0]/2) + 12.0 + np.log(np.abs(X[:, 0])*10 + 1)"
        elif test == 'simple4':
            eval_str = "1.0 + 3*X[:, 0]**2 - 0.5*X[:, 0]**3 + 0.1*X[:, 0]**4"
        elif test == 'simple5':
            eval_str = "(np.exp(X[:, 3]) + 3)/(np.abs(X[:, 1]) + np.cos(X[:, 0]) + 1.1)"

        X = np.random.randn(100, 5)*3
        y = eval(eval_str)
        print("Running on", eval_str)

    pkg_directory = '/'.join(__file__.split('/')[:-2] + ['julia'])

    def_hyperparams = ""

    # Add pre-defined functions to Julia
    for op_list in [binary_operators, unary_operators]:
        for i in range(len(op_list)):
            op = op_list[i]
            if '(' not in op:
                continue

            def_hyperparams += op + "\n"
            # Cut off from the first non-alphanumeric char:
            first_non_char = [
                    j for j in range(len(op))
                    if not (op[j].isalpha() or op[j].isdigit())][0]
            function_name = op[:first_non_char]
            op_list[i] = function_name

    number_total_procs = procs
    if slurm_cluster:
        number_total_procs *= cluster_nodes
    def_hyperparams += f"""include("{pkg_directory}/operators.jl")
const binops = {'[' + ', '.join(binary_operators) + ']'}
const unaops = {'[' + ', '.join(unary_operators) + ']'}
const ns=10;
const parsimony = {parsimony:f}f0
const alpha = {alpha:f}f0
const maxsize = {maxsize:d}
const migration = {'true' if migration else 'false'}
const hofMigration = {'true' if hofMigration else 'false'}
const fractionReplacedHof = {fractionReplacedHof}f0
const shouldOptimizeConstants = {'true' if shouldOptimizeConstants else 'false'}
const hofFile = "{equation_file}"
const nprocs = {number_total_procs:d}
const npopulations = {populations:d}
const nrestarts = {nrestarts:d}
const perturbationFactor = {perturbationFactor:f}f0
const annealing = {"true" if annealing else "false"}
const weighted = {"true" if weights is not None else "false"}
const mutationWeights = [
    {weightMutateConstant:f},
    {weightMutateOperator:f},
    {weightAddNode:f},
    {weightInsertNode:f},
    {weightDeleteNode:f},
    {weightSimplify:f},
    {weightRandomize:f},
    {weightDoNothing:f}
]
    """

    if X.shape[1] == 1:
        X_str = 'transpose([' + str(X.tolist()).replace(']', '').replace(',', '').replace('[', '') + '])'
    else:
        X_str = str(X.tolist()).replace('],', '];').replace(',', '')
    y_str = str(y.tolist())

    def_datasets = """const X = convert(Array{Float32, 2}, """f"{X_str})""""
const y = convert(Array{Float32, 1}, """f"{y_str})"

    if weights is not None:
        weight_str = str(weights.tolist())
        def_datasets += """
const weights = convert(Array{Float32, 1}, """f"{weight_str})"

    with open(f'{pkg_directory}/.hyperparams_{rand_string}.jl', 'w') as f:
        print(def_hyperparams, file=f)

    with open(f'{pkg_directory}/.dataset_{rand_string}.jl', 'w') as f:
        print(def_datasets, file=f)

    with open(f'{pkg_directory}/.runfile_{rand_string}.jl', 'w') as f:
        if slurm_cluster:
            print(f'const cpus_per = {procs:d}', file=f)
            print(f'const np = {cluster_nodes}', file=f)
            print(f'include("{pkg_directory}/slurm.jl")', file=f)
        print(f'@everywhere include("{pkg_directory}/.hyperparams_{rand_string}.jl")', file=f)
        print(f'@everywhere include("{pkg_directory}/.dataset_{rand_string}.jl")', file=f)
        print(f'@everywhere include("{pkg_directory}/sr.jl")', file=f)
        print('println("Loaded all files!")', file=f)
        print(f'fullRun({niterations:d}, npop={npop:d}, ncyclesperiteration={ncyclesperiteration:d}, fractionReplaced={fractionReplaced:f}f0, verbosity=round(Int32, {verbosity:f}), topn={topn:d})', file=f)
        print(f'rmprocs()', file=f)

    if not slurm_cluster:
        command = [
            f'julia -O{julia_optimization:d}',
            f'-p {procs}',
            f'{pkg_directory}/.runfile_{rand_string}.jl',
            ]

        if timeout is not None:
            command = [f'timeout {timeout}'] + command

        cur_cmd = ' '.join(command)
        print("Running on", cur_cmd)
        os.system(cur_cmd)
        try:
            output = pd.read_csv(equation_file, sep="|")
        except FileNotFoundError:
            print("Couldn't find equation file!")
            output = pd.DataFrame()
        return output
    else:
        # Don't run from python.
        return f'{pkg_directory}/.runfile_{rand_string}.jl'
