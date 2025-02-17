import time
import math

from .config import DEBUG
from .utils.colorify import print_normalize


# Possible stop strategies
STOPPING_STRATEGIES = {'iteration', 'tolerance', 'callback'}

EPS = 1e-10
PATIENCE = 3


# Define some constants
# TODO: better parametrize this?
MAX_ITER = int(1e12)
MIN_TOL = 1e-15

RHO = 1.5
RHO_INC = 1.2  # multiplicative update if rho is too small


class StoppingCriterion():
    """Class to check if we need to stop an algorithm.

    This base class will check for the timeout and the max_run.
    It should be sub-classed to check for the convergence of the algorithm.

    This class also handles the detection of diverging solvers and prints the
    progress if given a ``prgress_str``.

    Instances of this class should only be created with `cls._get_instance`,
    to make sure the class holds the proper attirbutes. This factory mechanism
    allow for easy subclassing without requesting to call the `super.__init___`
    in the subclass.

    Similarly, sub-classes should implement `check-convergence` to check if the
    algorithm has converged. This function will be called internally as a hook
    in `should_stop_solver`, which also handles `timeout`, `max_runs` and
    plateau detection.

    Parameters
    ----------
    **kwargs : dict
        All parameters passed when instanciating the StoppingCriterion. This
        will be used to re-create the criterion with extra arguments in the
        runner.
    strategy : str in {'iteration', 'tolerance', 'callback'}
        How the different precision solvers are called. Can be one of:
        - ``'iteration'``: call the run method with max_iter number increasing
        logarithmically to get more an more precise points.
        - ``'tolerance'``: call the run method with tolerance deacreasing
        logarithmically to get more and more precise points.
        - ``'callback'``: call the run method with a callback that will compute
        the objective function on a logarithmic scale. After each iteration,
        the callback should be called with the current iterate solution.
    """
    kwargs = None

    def __init__(self, strategy=None, **kwargs):

        assert strategy in STOPPING_STRATEGIES, (
            f"strategy should be in {STOPPING_STRATEGIES}. Got '{strategy}'."
        )

        self.kwargs = kwargs
        self.strategy = strategy

    def get_runner_instance(self, max_runs=1, timeout=None, progress_str=None,
                            solver=None):
        """Copy the stopping criterion and set the parameters that depends on
        how benchopt runner is called.

        Parameters
        ----------
        max_runs : int
            The maximum number of solver runs to perform to estimate
            the convergence curve.
        timeout : float
            The maximum duration in seconds of the solver run.
        progress_str : str or None
            Format string to display the progress of the solver.
        solver : BaseSolver
            The solver for which this stopping criterion is called. Used to get
            overridden ``stopping_strategy`` and ``get_next``.

        Returns
        -------
        stopping_criterion : StoppingCriterion
            The stopping criterion instance to use in the runner, with
            correct timeout and max_runs parameters.
        """

        # Check that the super constructor is correctly called in the
        # sub-classes
        if self.kwargs is None:
            raise ValueError(
                f"{self.__class__.__name__} is a subclass of StoppingCriterion"
                " but did not called super().__init__(**kwargs) with all its "
                "parameters in its constructor. See XXX for details on how "
                "to implement a new StoppingCriterion."
            )

        # Get strategy from solver
        strategy = solver._solver_strategy
        assert strategy in STOPPING_STRATEGIES, (
            f"stopping_strategy should be in {STOPPING_STRATEGIES}. "
            f"Got '{strategy}'."
        )

        # Create a new instance of the class
        stopping_criterion = self.__class__(
            **self.kwargs, strategy=strategy
        )

        # Set stopping criterion parameters depending on run parameters
        stopping_criterion.rho = RHO
        stopping_criterion.timeout = timeout
        stopping_criterion.max_runs = max_runs
        stopping_criterion.progress_str = progress_str
        stopping_criterion.solver = solver

        # Override get_next_stop_val if ``get_next`` is implemented for solver.
        if hasattr(solver, 'get_next'):
            assert (
                callable(solver.get_next)
                # and type(solver.get_next) == staticmethod
            ), "if defined, get_next should be a static method of the solver."
            try:
                solver.get_next(0)
            except TypeError:
                raise ValueError(
                    "get_next(0) throw a TypeError. Verify that `get_next` "
                    "signature is get_next(stop_val) and that it is "
                    "a staticmethod."
                )

            stopping_criterion.get_next_stop_val = solver.get_next

        # Store running arguments
        if timeout is not None:
            stopping_criterion._deadline = time.time() + timeout
        else:
            stopping_criterion._deadline = None
        stopping_criterion._prev_objective_value = 1e100

        return stopping_criterion

    def should_stop_solver(self, stop_val, cost_curve):
        """Base call to check if we should stop running a solver.

        This base call checks for the timeout and the max number of runs.
        It also notifies the runner if the curve is too flat, to increase
        the number of points between 2 evaluations of the objective.

        Parameters
        ----------
        stop_val : int | float
            Corresponds to stopping criterion of the underlying algorithm, such
            as ``tol`` or ``max_iter``.
        cost_curve : list of dict
            List of dict containing the values associated to the objective at
            each evaluated points.

        Returns
        -------
        stop : bool
            Whether or not we should stop the algorithm.
        status : str
            Reason why the algorithm was stopped if stop is True.
        next_stop_val : int | float
            Next value for the stopping criterion. This value depends on the
            stop strategy for the solver.
        """
        # Modify the criterion state:
        # - compute the number of run with the curve. We need to remove 1 as
        #   it contains the initial evaluation.
        # - compute the delta_objective for debugging and stalled progress.
        n_eval = len(cost_curve) - 1
        objective_value = cost_curve[-1]['objective_value']
        delta_objective = self._prev_objective_value - objective_value
        self._prev_objective_value = objective_value

        # default value for is_flat
        is_flat = False

        # check the different conditions:
        #     timeout / max_runs / diverging / stopping_criterion
        if self._deadline is not None and time.time() > self._deadline:
            stop = True
            status = 'timeout'

        elif n_eval == self.max_runs:
            stop = True
            status = 'max_runs'

        elif delta_objective < -1e10:
            stop = True
            status = 'diverged'

        else:
            # Call the sub-class hook, used to check stopping criterion
            # on the curve.
            stop, progress = self.check_convergence(cost_curve)

            # Display the progress if necessary
            progress = max(n_eval / self.max_runs, progress)
            self.show_progress(progress=progress)

            # Compute status and notify the runner if the curve is flat.
            status = 'done' if stop else None
            is_flat = delta_objective == 0

        if stop and DEBUG:
            print(f"DEBUG - Exit with delta_objective = {delta_objective:.2e} "
                  f"and n_eval={n_eval:.1e}.")

        if is_flat:
            self.rho *= RHO_INC
            if DEBUG:
                print("DEBUG - curve is flat -> increasing rho:", self.rho)

        return stop, status, self.get_next_stop_val(stop_val)

    def show_progress(self, progress):
        """Display progress in the CLI interface."""
        if self.progress_str is not None:
            if isinstance(progress, float):
                progress = f'{progress:6.1%}'
            print_normalize(
                self.progress_str.format(progress=progress),
                endline=False
            )

    def check_convergence(self, cost_curve):
        """Check if the solver should be stopped based on the objective curve.

        Parameters
        ----------
        cost_curve : list of dict
            List of dict containing the values associated to the objective at
            each evaluated points.

        Returns
        -------
        stop : bool
            Whether or not we should stop the algorithm.
        progress : float
            Measure of how far the solver is from convergence.
            This should be in [0, 1], 0 meaning no progress and 1 meaning
            that the solver has converged.
        """
        return False, 0

    @staticmethod
    def _reconstruct(klass, kwargs, runner_kwargs):
        criterion = klass(**kwargs)
        return criterion.get_runner_instance(**runner_kwargs)

    def __reduce__(self):
        kwargs = dict(
            strategy=self.strategy, **self.kwargs
        )
        runner_kwargs = dict(
            max_runs=self.max_runs, timeout=self.timeout,
            progress_str=self.progress_str, solver=self.solver
        )
        return self._reconstruct, (self.__class__, kwargs, runner_kwargs)

    def get_next_stop_val(self, stop_val):
        if self.strategy == "tolerance":
            return min(1, max(stop_val / self.rho, MIN_TOL))
        else:
            return max(stop_val + 1, min(int(self.rho * stop_val), MAX_ITER))


class SufficientDescentCriterion(StoppingCriterion):
    """Stopping criterion based on sufficient descent.

    The solver will be stopped once successive evaluations do not make enough
    progress. The number of successive evaluation and the definition of
    sufficient progress is controled by ``eps`` and ``patience``.

    Parameters
    ----------
    eps :  float (default: benchopt.stopping_criterion.EPS)
        The objective function change is considered as insufficient when it is
        in the interval ``[-eps, eps]``.
    patience :  float (default: benchopt.stopping_criterion.PATIENCE)
        The solver is stopped after ``patience`` successive insufficient
        updates.
    strategy : str in {'iteration', 'tolerance', 'callback'}
        How the different precision solvers are called. Can be one of:
        - ``'iteration'``: call the run method with max_iter number increasing
        logarithmically to get more an more precise points.
        - ``'tolerance'``: call the run method with tolerance deacreasing
        logarithmically to get more and more precise points.
        - ``'callback'``: call the run method with a callback that will compute
        the objective function on a logarithmic scale. After each iteration,
        the callback should be called with the current iterate solution.
    """

    def __init__(self, eps=EPS, patience=PATIENCE, strategy='iteration'):
        self.eps = eps
        self.patience = patience

        self._delta_objectives = []
        self._objective_value = 1e100

        super().__init__(
            eps=eps, patience=patience, strategy=strategy
        )

    def check_convergence(self, cost_curve):
        """Check if the solver should be stopped based on the objective curve.

        Parameters
        ----------
        cost_curve : list of dict
            List of dict containing the values associated to the objective at
            each evaluated points.

        Returns
        -------
        stop : bool
            Whether or not we should stop the algorithm.
        progress : float
            Measure of how far the solver is from convergence.
            This should be in [0, 1], 0 meaning no progress and 1 meaning
            that the solver has converged.
        """
        # Compute the current objective
        objective_value = cost_curve[-1]['objective_value']
        delta_objective = self._objective_value - objective_value
        self._objective_value = objective_value

        # Store only the last ``patience`` values for progress
        self._delta_objectives.append(delta_objective)
        if len(self._delta_objectives) > self.patience:
            self._delta_objectives.pop(0)

        delta = max(self._delta_objectives)
        if (-self.eps <= delta <= self.eps):
            return True, 1

        progress = math.log(max(abs(delta), self.eps)) / math.log(self.eps)
        return False, progress


class SufficientProgressCriterion(StoppingCriterion):
    """Stopping criterion based on sufficient progress.

    The solver will be stopped once successive evaluations do not make enough
    progress. The number of successive evaluation and the definition of
    sufficient progress is controled by ``eps`` and ``patience``.

    Parameters
    ----------
    eps :  float (default: benchopt.stopping_criterion.EPS)
        The progress between two steps is considered as insufficient when it is
        smaller than ``eps``.
    patience :  float (default: benchopt.stopping_criterion.PATIENCE)
        The solver is stopped after ``patience`` successive insufficient
        updates.
    strategy : str in {'iteration', 'tolerance', 'callback'}
        How the different precision solvers are called. Can be one of:
        - ``'iteration'``: call the run method with max_iter number increasing
        logarithmically to get more an more precise points.
        - ``'tolerance'``: call the run method with tolerance deacreasing
        logarithmically to get more and more precise points.
        - ``'callback'``: call the run method with a callback that will compute
        the objective function on a logarithmic scale. After each iteration,
        the callback should be called with the current iterate solution.
    """

    def __init__(self, eps=EPS, patience=PATIENCE, strategy='iteration'):
        self.eps = eps
        self.patience = patience

        self._progress = []
        self._best_objective_value = 1e100

        super().__init__(
            eps=eps, patience=patience, strategy=strategy
        )

    def check_convergence(self, cost_curve):
        """Check if the solver should be stopped based on the objective curve.

        Parameters
        ----------
        cost_curve : list of dict
            List of dict containing the values associated to the objective at
            each evaluated points.

        Returns
        -------
        stop : bool
            Whether or not we should stop the algorithm.
        progress : float
            Measure of how far the solver is from convergence.
            This should be in [0, 1], 0 meaning no progress and 1 meaning
            that the solver has converged.
        """
        # Compute the current objective and update best value
        objective_value = cost_curve[-1]['objective_value']
        delta_objective = self._best_objective_value - objective_value
        self._best_objective_value = min(
            objective_value, self._best_objective_value
        )

        # Store only the last ``patience`` values for progress
        self._progress.append(delta_objective)
        if len(self._progress) > self.patience:
            self._progress.pop(0)

        delta = max(self._progress)
        if delta <= self.eps * self._best_objective_value:
            return True, 1

        progress = math.log(max(abs(delta), self.eps)) / math.log(self.eps)
        return False, progress
