# === Start Python 2/3 compatibility
from __future__ import absolute_import, division, print_function, unicode_literals
from future.builtins import *  # noqa  pylint: disable=W0401, W0614
from future.builtins.disabled import *  # noqa  pylint: disable=W0401, W0614

# === End Python 2/3 compatibility

import re

# TODO: Python 3 workaround
try:
    from pathlib import Path
except ImportError:
    from pathlib2 import Path


class ProcessingType(object):
    """Baseclass for a pipeline processing type."""

    # Must be set externally before using
    root_path = None

    def __init__(self, revision, create=False):

        self.revision = revision

        # Run the create hook if specified
        if create:
            self._create()

        # Run the load hook if specified
        self._load()

    def _create(self):
        """Implement to add custom behaviour when a revision is created."""
        pass

    def _load(self):
        """Implement to add custom behaviour when a revision is loaded."""
        pass

    def job_script(self, tag):
        """The slurm job script to queue up."""
        return None

    def pipeline_script(self):
        """The slurm jobscript to queue up."""
        raise NotImplementedError("""Must be implemented in derived type.""")

    def ls(self):
        """Find all matching data.

        Returns
        -------
        tags : list
            Return the tags of all outputs found.
        """

        base = self.base_path

        if not base.exists():
            raise ValueError("Base path %s does not exist." % base)

        file_regex = re.compile("^%s$" % self.tag_pattern)

        entries = [path.name for path in base.glob("*") if file_regex.match(path.name)]

        return sorted(entries)

    @classmethod
    def ls_type(cls, existing=True):
        """List all processing types found.

        Parameters
        ----------
        existing : bool, optional
            Only return types that have existing data.

        Returns
        -------
        type_names : list
        """

        type_names = [t.type_name for t in all_subclasses(cls)]

        if existing:
            base = Path(cls.root_path)
            return sorted([t.name for t in base.glob("*") if t.name in type_names])
        else:
            return type_names

    @classmethod
    def ls_rev(cls):
        """List all existing revisions of this type.

        Returns
        -------
        rev : list
            List of revision names
        """

        base = Path(cls.root_path) / cls.type_name

        # Revisions are labelled by a two digit code
        # TODO: decide if two digits (i.e. 100 revisions max is enough)
        rev_regex = re.compile("^rev_\d{2}$")

        return sorted([t.name for t in base.glob("*") if rev_regex.match(t.name)])

    @classmethod
    def create_rev(cls):
        """Create a new revision of this type."""

        revisions = cls.ls_rev()

        if revisions:
            last_rev = revisions[-1].split("_")[-1]
            new_rev = "rev_%02i" % (int(last_rev) + 1)
        else:
            new_rev = "rev_00"

        (Path(cls.root_path) / cls.type_name / new_rev).mkdir(parents=True)

        return cls(new_rev, create=True)

    def queued(self):
        """Get the queued and running jobs of this type.

        Returns
        -------
        waiting : list
            List of jobs that are waiting to run.
        running : list
            List of running jobs.
        """

        job_regex = re.compile("^%s$" % self.job_name(self.tag_pattern))

        # Find matching jobs
        jobs = [job for job in slurm_jobs() if job_regex.match(job["NAME"])]

        running = [job["NAME"].split("/")[-1] for job in jobs if job["ST"] == "R"]
        waiting = [job["NAME"].split("/")[-1] for job in jobs if job["ST"] == "PD"]

        return waiting, running

    def job_name(self, tag):
        """The job name used to run the tag.

        Parameters
        ----------
        tag : str
            Tag for the job.

        Returns
        -------
        jobname : str
        """
        return "chp/%s/%s/%s" % (self.type_name, self.revision, tag)

    @property
    def base_path(self):
        """Base path for this processed data type."""

        base_path = Path(self.root_path) / self.type_name / self.revision

        return base_path

    def available(self):
        """Return the list of tags that we can generate given current data.

        This can (and should) include tags that have already been processed
        if the prerequites are still available.

        Returns
        -------
        tags : list of strings
            A list of all the tags that could be generated.
        """
        pass

    @classmethod
    def latest(cls):
        """Create an instance to manage the latest revision.

        Returns
        -------
        pt : cls
            An instance of the processing type for the latest revision.
        """

        rev = cls.ls_rev()

        if not rev:
            raise RuntimeError("No revisions of type %s exist." % cls.type_name)

        # Create instance and set the revision
        return cls(rev[-1])

    def generate(self, max=10, submit=True):
        """Queue up jobs that are available to run.

        Parameters
        ----------
        max : int, optional
            The maximum number of jobs to submit at once.
        submit : bool, optional
            Submit the jobs to the queue.
        """

        to_run = self.pending()[:max]

        for tag in to_run:
            queue_job(self.job_script(tag), submit=submit)

    def pending(self):
        """Jobs available to run."""

        waiting, running = self.queued()
        pending = set(self.available()).difference(self.ls(), waiting, running)

        return sorted(list(pending))


def queue_job(script, submit=True):
    """Queue a pipeline script given as a string."""

    import os
    import tempfile

    with tempfile.NamedTemporaryFile("w+r") as fh:
        fh.write(script)
        fh.flush()

        # TODO: do this in a better way
        if submit:
            cmd = "caput-pipeline queue %s"
        else:
            cmd = "caput-pipeline queue --nosubmit %s"
        os.system(cmd % fh.name)


def slurm_jobs(user=None):
    """Get the jobs of the given user.

    Parameters
    ----------
    user : str, optional
        User to fetch the slurm jobs of. If not set, use the current user.

    Returns
    -------
    jobs : list
        List of dictionaries giving the jobs state.
    """

    import subprocess as sp

    if user is None:
        import getpass

        user = getpass.getuser()

    # Call squeue to get the users jobs and get it's stdout
    try:
        process = sp.Popen(
            ["squeue", "-u", user, "-o", "%all"],
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            shell=False,
            universal_newlines=True,
        )
        proc_stdout, proc_stderr = process.communicate()
        lines = proc_stdout.split("\n")
    except OSError:
        import warnings

        warnings.warn('Failure running "squeue".')
        return []

    # Extract the headers
    header_line = lines.pop(0)
    header_cols = header_line.split("|")

    def slurm_split(line):
        # Split an squeue line accounting for the partitions

        tokens = line.split("|")

        fields = []

        t = None
        for token in tokens:
            t = token if t is None else t + "|" + token

            # Check if the token is balanced with square brackets
            br = t.count("[") - t.count("]")

            # If balanced keep the whole token, otherwise we keep will just
            # continue to see if the next token balances it
            if br == 0:
                fields.append(t)
                t = None

        return fields

    # Iterate over the following entries and parse them into queue jobs
    entries = []
    error_lines = []  # do something with this later
    for line in lines:
        parts = slurm_split(line)
        d = {}

        if len(parts) != len(header_cols):
            error_lines.append((len(parts), line, parts))
        else:
            for i, key in enumerate(header_cols):
                d[key] = parts[i]
            entries.append(d)

    return entries


def all_subclasses(cls):
    """Recursively find all subclasses of cls."""

    subclasses = []

    stack = [cls]
    while stack:
        cls = stack.pop()

        for c in cls.__subclasses__():
            subclasses.append(c)
            stack.append(c)

    return subclasses
