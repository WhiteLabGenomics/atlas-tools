import logging
import multiprocessing
import os
import platform
import re
import urllib.parse
from typing import cast

from .build_state import CensusBuildArgs
from .logging import logging_init


def urljoin(base: str, url: str) -> str:
    """
    like urllib.parse.urljoin, but doesn't get confused by S3://
    """
    p_url = urllib.parse.urlparse(url)
    if p_url.netloc:
        return url

    p_base = urllib.parse.urlparse(base)
    path = urllib.parse.urljoin(p_base.path, p_url.path)
    parts = [
        p_base.scheme,
        p_base.netloc,
        path,
        p_url.params,
        p_url.query,
        p_url.fragment,
    ]
    return urllib.parse.urlunparse(parts)


def urlcat(base: str, *paths: str) -> str:
    """
    Concat one or more paths, separated with '/'. Similar to urllib.parse.urljoin,
    but doesn't get confused by S3:// and other "non-standard" protocols (treats
    them as if they are same as http: or file:)

    Similar to urllib.parse.urljoin except it takes an iterator, and
    assumes the container_uri is a 'directory'/container, ie, ends in '/'.
    """

    url = base
    for p in paths:
        url = url if url.endswith("/") else url + "/"
        url = urljoin(url, p)
    return url


def env_var_init() -> None:
    """
    Set environment variables as needed by dependencies, etc.

    This controls thread allocation for worker (child) processes. It is executed too
    late to influence __init__ time thread pool allocations for the main process.
    """

    # Each of these control thread-pool allocation for commonly used packages that
    # may be pulled into our environment, and which have import-time pool allocation.
    # Most do import time thread pool allocation equal to host CPU count, which can
    # result in excessive unused thread pools on high CPU machines.
    #
    # Where we are confident we have no performance dependency related to their concurrency,
    # set their pool size to "1". Otherwise set to something useful.
    #
    # OMP_NUM_THREADS: OpenMp,
    # OPENBLAS_NUM_THREADS: OpenBLAS,
    # MKL_NUM_THREADS: Intel MKL,
    # VECLIB_MAXIMUM_THREADS: Accelerate,
    # NUMEXPR_NUM_THREADS: NumExpr

    if "NUMEXPR_MAX_THREADS" not in os.environ:
        # ref: https://numexpr.readthedocs.io/en/latest/user_guide.html#threadpool-configuration
        # In particular, the docs state that >8 threads is not helpful except in extreme circumstances.
        val = str(min(8, max(1, cpu_count() // 2)))
        os.environ["NUMEXPR_MAX_THREADS"] = val
        logging.info(f'Setting NUMEXPR_MAX_THREADS environment variable to "{val}"')

    for env_name in [
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ]:
        if env_name not in os.environ:
            logging.info(f'Setting {env_name} environment variable to "1"')
            os.environ[env_name] = "1"


def process_init(args: CensusBuildArgs) -> None:
    """
    Called on every process start to configure global package/module behavior.
    """
    logging_init(args)

    if multiprocessing.get_start_method(True) != "spawn":
        multiprocessing.set_start_method("spawn", True)

    env_var_init()

    # these are super noisy!
    numba_logger = logging.getLogger("numba")
    numba_logger.setLevel(logging.WARNING)
    h5py_logger = logging.getLogger("h5py")
    h5py_logger.setLevel(logging.WARNING)


class ProcessResourceGetter:
    """
    Access to process resource state, primary for diagnostic/debugging purposes. Currently
    provides current and high water mark for:
    * thread count
    * mmaps
    * major page faults

    Linux-only at the moment.
    https://docs.kernel.org/filesystems/proc.html
    """

    # historical maxima
    max_thread_count = -1
    max_map_count = -1

    @property
    def thread_count(self) -> int:
        """Return the thread count for the current process. Retain the historical maximum."""
        if platform.system() != "Linux":
            return -1

        with open("/proc/self/status") as f:
            status = f.read()
            thread_count = int(re.split(r".*\nThreads:\t(\d+)\n.*", status)[1])
            self.max_thread_count = max(thread_count, self.max_thread_count)
        return thread_count

    @property
    def map_count(self) -> int:
        """Return the memory map count for the current process. Retain the historical maximum."""
        if platform.system() != "Linux":
            return -1

        with open("/proc/self/maps") as f:
            maps = f.read()
            map_count = maps.count("\n")
            self.max_map_count = max(map_count, self.max_map_count)
        return map_count

    @property
    def majflt(self) -> tuple[int, int]:
        """Return the major faults and cumulative major faults (includes children) for current process."""
        if platform.system() != "Linux":
            return (-1, -1)

        with open("/proc/self/stat") as f:
            stats = f.read()
            stats_fields = stats.split()

        return int(stats_fields[11]), int(stats_fields[12])


_resource_getter = ProcessResourceGetter()


def log_process_resource_status(preface: str = "Resource use:", level: int = logging.DEBUG) -> None:
    """Print current and historical max of thread and (memory) map counts"""
    if platform.system() == "Linux":
        logging.log(
            level,
            f"{preface} threads: {_resource_getter.thread_count} "
            f"[max: {_resource_getter.max_thread_count}], "
            f"maps: {_resource_getter.map_count} "
            f"[max: {_resource_getter.max_map_count}], "
            f"page faults (cumm): {_resource_getter.majflt[1]}",
        )


def cpu_count() -> int:
    """
    os.cpu_count() returns None if "undetermined" number of CPUs.
    This function exists to always return a default of `1` when
    os.cpu_count returns None.
    """
    cpu_count = os.cpu_count()
    if os.cpu_count() is None:
        return 1
    return cast(int, cpu_count)
