"A context manager to handle exception with option to open a debugger."
from contextlib import contextmanager

from .colorify import colorify
from .colorify import RED, YELLOW
from .colorify import print_normalize


# Get config values
from ..config import DEBUG


@contextmanager
def exception_handler(tag=None, pdb=False):
    """Context manager to handle exception with option to open a debugger.

    Parameter
    ---------
    tag: str
        Name to display before outputing error in red.
    pdb: bool
        If set to True, open a debugger if an error is raised.
    """
    try:
        yield
    except KeyboardInterrupt:
        status = colorify("interrupted", YELLOW)
        print_normalize(f"\r{tag} {status}")
        raise SystemExit(1)
    except BaseException:
        status = colorify("error", RED)
        print_normalize(f"{tag} {status}")

        if pdb:
            # Use ipdb if it is available and default to pdb otherwise.
            try:
                from ipdb import post_mortem
            except ImportError:
                from pdb import post_mortem
            post_mortem()

        if DEBUG:
            raise
        else:
            import traceback
            traceback.print_exc()
