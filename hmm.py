import abc
import functools
import itertools
import logging
import time

import numpy as np
import scipy as sp
import scipy.cluster

from generative_model import GenerativeModel
from gmm import *
from gmm import _distribute_covar_matrix_to_match_cvtype, _validate_covars
import hmm_trainers

ZEROLOGPROB = -1e200

log = logging.getLogger('gm.hmm')

def HMM(emission_type='gaussian', *args, **kwargs):
    """Create an HMM object with the given emission_type."""
    supported_emission_types = dict([(x.emission_type, x)
                                     for x in _BaseHMM.__subclasses__()])
    if emission_type in supported_emission_types.keys():
        return supported_emission_types[emission_type](*args, **kwargs)
    else:
        raise ValueError, 'Unknown emission_type'
    

class _BaseHMM(GenerativeModel):
    """Hidden Markov Model abstract base class.
    
    Representation of a hidden Markov model probability distribution.
    This class allows for easy evaluation of, sampling from, and
    maximum-likelihood estimation of the parameters of a HMM.

    See the instance documentation for details specific to a
    particular object.

    Attributes
    ----------
    nstates : int (read-only)
        Number of states in the model.
    transmat : array, shape (`nstates`, `nstates`)
        Matrix of transition probabilities between states.
    startprob : array, shape ('nstates`,)
        Initial state occupation distribution.
    labels : list, len `nstates`
        Optional labels for each state.

    Methods
    -------
    eval(obs)
        Compute the log likelihood of `obs` under the HMM.
    decode(obs)
        Find most likely state sequence for each point in `obs` using the
        Viterbi algorithm.
    rvs(n=1)
        Generate `n` samples from the HMM.
    init(obs)
        Initialize HMM parameters from `obs`.
    train(obs)
        Estimate HMM parameters from `obs` using the Baum-Welch algorithm.

    See Also
    --------
    gmm : Gaussian mixture model
    """
    __metaclass__ = abc.ABCMeta

    # This class implements the public interface to all HMMs that
    # derive from it, including all of the machinery for the
    # forward-backward and Viterbi algorithms.  Subclasses need only
    # implement the abstractproperty emission_type, and the
    # abstractmethods _generate_sample_from_state(),
    # _compute_log_likelihood(), _init(), and a corresponding
    # HMMTrainer instance, all of which depend on the specific
    # emission distribution.
    #
    # Subclasses will probably also want to implement properties for
    # the emission distribution parameters to expose them publically.

    @abc.abstractproperty
    def emission_type(self):
        """String identifier for the emission distribution used by this HMM"""
        return None

    def __init__(self, nstates=1, startprob=None, transmat=None,
        labels=None, trainer=hmm_trainers.BaseHMMBaumWelchTrainer()):
        self._nstates = nstates

        if startprob is None:
            startprob = np.tile(1.0 / nstates, nstates)
        self.startprob = startprob

        if transmat is None:
            transmat = np.tile(1.0 / nstates, (nstates, nstates))
        self.transmat = transmat

        if labels is None:
            labels = [None] * nstates
        self.labels = labels

        self.trainer = trainer

    def eval(self, obs, maxrank=None, beamlogprob=-np.Inf):
        """Compute the log probability under the model and compute posteriors

        Implements rank and beam pruning in the forward-backward
        algorithm to speed up inference in large models.

        Parameters
        ----------
        obs : array_like, shape (n, ndim)
            Sequence of ndim-dimensional data points.  Each row
            corresponds to a single point in the sequence.
        maxrank : int
            Maximum rank to evaluate for rank pruning.  If not None,
            only consider the top `maxrank` states in the inner
            sum of the forward algorithm recursion.  Defaults to None
            (no rank pruning).  See The HTK Book for more details.
        beamlogprob : float
            Width of the beam-pruning beam in log-probability units.
            Defaults to -numpy.Inf (no beam pruning).  See The HTK
            Book for more details.

        Returns
        -------
        logprob : array_like, shape (n,)
            Log probabilities of the sequence `obs`
        posteriors: array_like, shape (n, nstates)
            Posterior probabilities of each state for each
            observation

        See Also
        --------
        lpdf : Compute the log probability under the model
        decode : Find most likely state sequence corresponding to a `obs`
        """
        framelogprob = self._compute_log_likelihood(obs)
        logprob, fwdlattice = self._do_forward_pass(framelogprob, maxrank,
                                                    beamlogprob)
        bwdlattice = self._do_backward_pass(framelogprob, fwdlattice, maxrank,
                                            beamlogprob)
        gamma = fwdlattice + bwdlattice
        # gamma is guaranteed to be correctly normalized by logprob at
        # all frames, unless we do approximate inference using pruning.
        # So, we will normalize each frame explicitly in case we
        # pruned too aggressively.
        posteriors = np.exp(gamma.T - logsum(gamma, axis=1)).T
        return logprob, posteriors

    def lpdf(self, obs, maxrank=None, beamlogprob=-np.Inf):
        """Compute the log probability under the model.

        Parameters
        ----------
        obs : array_like, shape (n, ndim)
            Sequence of ndim-dimensional data points.  Each row
            corresponds to a single data point.
        maxrank : int
            Maximum rank to evaluate for rank pruning.  If not None,
            only consider the top `maxrank` states in the inner
            sum of the forward algorithm recursion.  Defaults to None
            (no rank pruning).  See The HTK Book for more details.
        beamlogprob : float
            Width of the beam-pruning beam in log-probability units.
            Defaults to -numpy.Inf (no beam pruning).  See The HTK
            Book for more details.

        Returns
        -------
        logprob : array_like, shape (n,)
            Log probabilities of each data point in `obs`

        See Also
        --------
        eval : Compute the log probability under the model and compute posteriors
        decode : Find most likely state sequence corresponding to a `obs`
        """
        framelogprob = self._compute_log_likelihood(obs)
        logprob, fwdlattice =  self._do_forward_pass(framelogprob, maxrank,
                                                     beamlogprob)
        return logprob

    def decode(self, obs, maxrank=None, beamlogprob=-np.Inf):
        """Find most likely state sequence corresponding to `obs`.

        Uses the Viterbi algorithm.

        Parameters
        ----------
        obs : array_like, shape (n, ndim)
            List of ndim-dimensional data points.  Each row corresponds to a
            single data point.
        maxrank : int
            Maximum rank to evaluate for rank pruning.  If not None,
            only consider the top `maxrank` states in the inner
            sum of the forward algorithm recursion.  Defaults to None
            (no rank pruning).  See The HTK Book for more details.
        beamlogprob : float
            Width of the beam-pruning beam in log-probability units.
            Defaults to -numpy.Inf (no beam pruning).  See The HTK
            Book for more details.

        Returns
        -------
        viterbi_logprob : float
            Log probability of the maximum likelihood path through the HMM
        components : array_like, shape (n,)
            Index of the most likelihood states for each observation

        See Also
        --------
        eval : Compute the log probability under the model and compute posteriors
        lpdf : Compute the log probability under the model
        """
        framelogprob = self._compute_log_likelihood(obs)
        logprob, state_sequence = self._do_viterbi_pass(framelogprob, maxrank,
                                                        beamlogprob)
        return logprob, state_sequence
        
    def rvs(self, n=1):
        """Generate random samples from the model.

        Parameters
        ----------
        n : int
            Number of samples to generate.

        Returns
        -------
        obs : array_like, length `n`
            List of samples
        """

        startprob_pdf = self.startprob
        startprob_cdf = np.cumsum(startprob_pdf)
        transmat_pdf = self.transmat
        transmat_cdf = np.cumsum(transmat_pdf, 1);

        # Initial state.
        rand = np.random.rand()
        currstate = (startprob_cdf > rand).argmax()
        obs = [self._generate_sample_from_state(currstate)]

        for x in xrange(n-1):
            rand = np.random.rand()
            currstate = (transmat_cdf[currstate] > rand).argmax()
            obs.append(self._generate_sample_from_state(currstate))

        return np.array(obs)

    def init(self, obs, params='stmc', **kwargs):
        """Initialize model parameters from data using the k-means algorithm

        Parameters
        ----------
        obs : list
            List of array-like observation sequences (shape (n_i, ndim)).
        params : string
            Controls which parameters are updated in the training
            process.  Can contain any combination of 's' for startprob,
            't' for transmat, 'm' for means, and 'c' for covars.
            Defaults to 'stmc'.
        **kwargs :
            Keyword arguments to pass through to the k-means function 
            (scipy.cluster.vq.kmeans2)

        See Also
        --------
        scipy.cluster.vq.kmeans2
        """
        self._init(obs, params, **kwargs)

    def train(self, obs, iter=10, thresh=1e-2, params='stmpc',
              maxrank=None, beamlogprob=-np.Inf, **kwargs):
        """Estimate model parameters with the Baum-Welch algorithm.

        Parameters
        ----------
        obs : list
            List of array-like observation sequences (shape (n_i, ndim)).
        iter : int
            Number of iterations to perform.
        thresh : float
            Convergence threshold.
        params : string
            Controls which parameters are updated in the training
            process.  Can contain any combination of 's' for startprob,
            't' for transmat, 'm' for means, and 'c' for covars, etc.
            Defaults to all parameters ('stmpc').
        maxrank : int
            Maximum rank to evaluate for rank pruning.  If not None,
            only consider the top `maxrank` states in the inner
            sum of the forward algorithm recursion.  Defaults to None
            (no rank pruning).  See "The HTK Book" for more details.
        beamlogprob : float
            Width of the beam-pruning beam in log-probability units.
            Defaults to -numpy.Inf (no beam pruning).  See "The HTK
            Book" for more details.

        Returns
        -------
        logprob : list
            Log probabilities of each data point in `obs` for each iteration
        """
        return self.trainer.train(self, obs, iter, thresh, params,
                                  maxrank, beamlogprob, **kwargs)

    @property
    def nstates(self):
        """Number of states in the model."""
        return self._nstates

    @property
    def startprob(self):
        """Mixing startprob for each state."""
        return np.exp(self._log_startprob)

    @startprob.setter
    def startprob(self, startprob):
        if len(startprob) != self._nstates:
            raise ValueError, 'startprob must have length nstates'
        if not almost_equal(np.sum(startprob), 1.0):
            raise ValueError, 'startprob must sum to 1.0'
        
        self._log_startprob = np.log(np.asarray(startprob).copy())

    @property
    def transmat(self):
        """Matrix of transition probabilities."""
        return np.exp(self._log_transmat)

    @transmat.setter
    def transmat(self, transmat):
        if np.asarray(transmat).shape != (self._nstates, self._nstates):
            raise ValueError, 'transmat must have shape (nstates, nstates)'
        if not np.all(almost_equal(np.sum(transmat, axis=1), 1.0)):
            raise ValueError, 'each row of transmat must sum to 1.0'
        
        self._log_transmat = np.log(np.asarray(transmat).copy())
        underflow_idx = np.isnan(self._log_transmat)
        self._log_transmat[underflow_idx] = -np.Inf

    @property
    def trainer(self):
        """HMMTrainer used to train this HMM."""
        return self._trainer

    @trainer.setter
    def trainer(self, trainer):
        if self.emission_type != trainer.emission_type:
            raise ValueError, 'trainer has incompatible emission_type'
        self._trainer = trainer

    def _do_viterbi_pass(self, framelogprob, maxrank=None, beamlogprob=-np.Inf):
        nobs = len(framelogprob)
        lattice = np.zeros((nobs, self._nstates))
        traceback = np.zeros((nobs, self._nstates), dtype=np.int) 

        lattice[0] = self._log_startprob + framelogprob[0]
        for n in xrange(1, nobs):
            idx = self._prune_states(lattice[n-1], maxrank, beamlogprob)
            pr = self._log_transmat[idx].T + lattice[n-1,idx]
            lattice[n]   = np.max(pr, axis=1) + framelogprob[n]
            traceback[n] = np.argmax(pr, axis=1)
        lattice[lattice <= ZEROLOGPROB] = -np.Inf;
        
        # Do traceback.
        reverse_state_sequence = []
        s = lattice[-1].argmax()
        for frame in reversed(traceback):
            reverse_state_sequence.append(s)
            s = frame[s]

        reverse_state_sequence.reverse()
        return logsum(lattice[-1]), np.array(reverse_state_sequence)

    def _do_forward_pass(self, framelogprob, maxrank=None, beamlogprob=-np.Inf):
        nobs = len(framelogprob)
        fwdlattice = np.zeros((nobs, self._nstates))

        fwdlattice[0] = self._log_startprob + framelogprob[0]
        for n in xrange(1, nobs):
            idx = self._prune_states(fwdlattice[n-1], maxrank, beamlogprob)
            fwdlattice[n] = (logsum(self._log_transmat[idx].T
                                    + fwdlattice[n-1,idx], axis=1)
                             + framelogprob[n])
        fwdlattice[fwdlattice <= ZEROLOGPROB] = -np.Inf

        return logsum(fwdlattice[-1]), fwdlattice

    def _do_backward_pass(self, framelogprob, fwdlattice, maxrank=None,
                          beamlogprob=-np.Inf):
        nobs = len(framelogprob)
        bwdlattice = np.zeros((nobs, self._nstates))

        for n in xrange(nobs - 1, 0, -1):
            # Do HTK style pruning (p. 137 of HTK Book version 3.4).
            # Don't bother computing backward probability if
            # fwdlattice * bwdlattice is more than a certain distance
            # from the total log likelihood.
            idx = self._prune_states(bwdlattice[n] + fwdlattice[n], None,
                                     -50)
                                     #beamlogprob)
                                     #-np.Inf)
            bwdlattice[n-1] = logsum(self._log_transmat[:,idx]
                                     + bwdlattice[n,idx] + framelogprob[n,idx],
                                     axis=1)
        bwdlattice[bwdlattice <= ZEROLOGPROB] = -np.Inf

        return bwdlattice

    def _prune_states(self, lattice_frame, maxrank, beamlogprob):
        """ Returns indices of the active states in `lattice_frame`
        after rank and beam pruning.
        """
        # Beam pruning
        threshlogprob = logsum(lattice_frame) + beamlogprob
        
        # Rank pruning
        if maxrank:
            # How big should our rank pruning histogram be?
            nbins = 3 * len(lattice_frame)

            lattice_min = lattice_frame[lattice_frame > ZEROLOGPROB].min() - 1
            hst, cdf = np.histogram(lattice_frame, bins=nbins, new=True,
                                    range=(lattice_min, lattice_frame.max()))
        
            # Want to look at the high ranks.
            hst = hst[::-1].cumsum()
            cdf = cdf[::-1]

            rankthresh = cdf[hst >= min(maxrank, self._nstates)].max()
      
            # Only change the threshold if it is stricter than the beam
            # threshold.
            threshlogprob = max(threshlogprob, rankthresh)
    
        # Which states are active?
        state_idx, = np.nonzero(lattice_frame >= threshlogprob)
        return state_idx

    @abc.abstractmethod
    def _compute_log_likelihood(self, obs):
        pass
    
    @abc.abstractmethod
    def _generate_sample_from_state(self, state):
        pass

    @abc.abstractmethod
    def _init(self, obs, params, **kwargs):
        if 's' in params:
            self.startprob[:] = 1.0 / self._nstates
        if 't' in params:
            self.transmat[:] = 1.0 / self._nstates


class GaussianHMM(_BaseHMM):
    """Hidden Markov Model with Gaussian emissions

    Representation of a hidden Markov model probability distribution.
    This class allows for easy evaluation of, sampling from, and
    maximum-likelihood estimation of the parameters of a HMM.

    Attributes
    ----------
    cvtype : string (read-only)
        String describing the type of covariance parameters used by
        the model.  Must be one of 'spherical', 'tied', 'diag', 'full'.
    ndim : int (read-only)
        Dimensionality of the Gaussian components.
    nstates : int (read-only)
        Number of states in the model.
    transmat : array, shape (`nstates`, `nstates`)
        Matrix of transition probabilities between states.
    startprob : array, shape ('nstates`,)
        Initial state occupation distribution.
    means : array, shape (`nstates`, `ndim`)
        Mean parameters for each state.
    covars : array
        Covariance parameters for each state.  The shape depends on
        `cvtype`:
            (`nstates`,)                if 'spherical',
            (`ndim`, `ndim`)            if 'tied',
            (`nstates`, `ndim`)         if 'diag',
            (`nstates`, `ndim`, `ndim`) if 'full'
    labels : list, len `nstates`
        Optional labels for each state.

    Methods
    -------
    eval(obs)
        Compute the log likelihood of `obs` under the HMM.
    decode(obs)
        Find most likely state sequence for each point in `obs` using the
        Viterbi algorithm.
    rvs(n=1)
        Generate `n` samples from the HMM.
    init(obs)
        Initialize HMM parameters from `obs`.
    train(obs)
        Estimate HMM parameters from `obs` using the Baum-Welch algorithm.

    Examples
    --------
    >>> hmm = HMM('gaussian', nstates=2, ndim=1)

    See Also
    --------
    gmm : Gaussian mixture model
    """

    emission_type = 'gaussian'

    def __init__(self, nstates=1, ndim=1, cvtype='diag',
                 startprob=None, transmat=None, labels=None,
                 means=None, covars=None,
                 trainer=hmm_trainers.GaussianHMMBaumWelchTrainer()):
        """Create a hidden Markov model with Gaussian emissions.

        Initializes parameters such that every state has zero mean and
        identity covariance.

        Parameters
        ----------
        ndim : int
            Dimensionality of the states.
        nstates : int
            Number of states.
        cvtype : string (read-only)
            String describing the type of covariance parameters to
            use.  Must be one of 'spherical', 'tied', 'diag', 'full'.
            Defaults to 'diag'.
        """
        super(GaussianHMM, self).__init__(nstates, startprob,
                                          transmat, labels, trainer)

        self._ndim = ndim
        self._cvtype = cvtype

        if means is None:
            means = np.zeros((nstates, ndim))
        self.means = means

        if covars is None:
            covars = _distribute_covar_matrix_to_match_cvtype(np.eye(ndim),
                                                              cvtype, nstates)
        self.covars = covars

        self.trainer = trainer

    # Read-only properties.
    @property
    def cvtype(self):
        """Covariance type of the model.

        Must be one of 'spherical', 'tied', 'diag', 'full'.
        """
        return self._cvtype

    @property
    def ndim(self):
        """Dimensionality of the states."""
        return self._ndim

    @property
    def means(self):
        """Mean parameters for each state."""
        return self._means

    @means.setter
    def means(self, means):
        means = np.asarray(means)
        if means.shape != (self._nstates, self._ndim):
            raise ValueError, 'means must have shape (nstates, ndim)'
        self._means = means.copy()

    @property
    def covars(self):
        """Covariance parameters for each state."""
        return self._covars

    @covars.setter
    def covars(self, covars):
        covars = np.asarray(covars)
        _validate_covars(covars, self._cvtype, self._nstates, self._ndim)
        self._covars = covars.copy()

    def _compute_log_likelihood(self, obs):
        return lmvnpdf(obs, self._means, self._covars, self._cvtype)

    def _generate_sample_from_state(self, state):
        if self._cvtype == 'tied':
            cv = self._covars
        else:
            cv = self._covars[state]
        return sample_gaussian(self._means[state], cv, self._cvtype)

    def _init(self, obs, params='stmc', **kwargs):
        super(GaussianHMM, self)._init(obs, params=params)

        if 'm' in params:
            self._means, tmp = sp.cluster.vq.kmeans2(obs[0], self._nstates,
                                                     **kwargs)
        if 'c' in params:
            cv = np.cov(obs[0].T)
            if not cv.shape:
                cv.shape = (1, 1)
            self._covars = _distribute_covar_matrix_to_match_cvtype(
                cv, self._cvtype, self._nstates)


class GMMHMM(_BaseHMM):
    emission_type = 'gmm'
