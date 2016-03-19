from __future__ import print_function
import numpy as np
import tensorflow as tf

from edward.stats import bernoulli, beta, norm, dirichlet, invgamma
from edward.util import get_dims, concat, Variable

class Likelihood:
    """
    Base class for variational likelihoods, q(z | lambda).
    """
    def __init__(self, num_vars):
        self.num_vars = num_vars
        self.num_params = None

    def mapping(self, x):
        """
        A global mapping from data point x -> lambda, the local
        variational parameters.

        In classical variational inference, the global mapping is
        parameterized by the collection of all variational parameters,
        and the output is simply the subset of relevant local
        variational parameters.

        In a slightly more complex scenario, such as for latent
        variables with constrained support, the mapping additionally
        includes a constrained transformation so that the parameters
        to be optimized live on the unconstrained space but the output
        of this mapping for use in the variational model has
        constrained latent variables.

        In non-trivial parameterizations such as inverse mappings in
        Helmholtz machines and variational auto-encoders, and
        parameter tying procedures in message passing, the mapping is
        a function of data point with a fixed number of parameters
        that does not grow with the data.

        Parameters
        ----------
        x : Data
            Data point

        Returns
        -------
        tf.Tensor
            A list where each element is a particular set of local parameters.
            TODO or maybe
            A dictionary of local variational parameter names and
            their outputted values.
        """
        raise NotImplementedError()

    def set_params(self, params):
        """
        This sets the parameters of the variational family, for use in
        other methods of the class.

        Parameters
        ----------
        params : list
            Each element in the list is a particular set of local parameters.
        """
        raise NotImplementedError()

    # TODO use __str__(self):
    def print_params(self, sess):
        raise NotImplementedError()

    def sample_noise(self, size):
        """
        eps = sample_noise() ~ s(eps)
        s.t. z = reparam(eps; lambda) ~ q(z | lambda)
        Returns
        -------
        np.ndarray
            n_minibatch x dim(lambda) array of type np.float32, where each
            row is a sample from q.
        Notes
        -----
        Unlike the other methods, this return object is a realization
        of a TensorFlow array. This is required as we rely on
        NumPy/SciPy for sampling from distributions.
        """
        raise NotImplementedError()

    def reparam(self, eps):
        """
        eps = sample_noise() ~ s(eps)
        s.t. z = reparam(eps; lambda) ~ q(z | lambda)
        """
        raise NotImplementedError()

    def sample(self, size, sess=None):
        """
        z ~ q(z | lambda)

        Parameters
        ----------
        sess : tf.Session, optional

        Returns
        -------
        np.ndarray
            n_minibatch x dim(z) array of type np.float32, where each
            row is a sample from q.

        Notes
        -----
        Unlike the other methods, this return object is a realization
        of a TensorFlow array. This is required as we rely on
        NumPy/SciPy for sampling from distributions.
        The method defaults to sampling noise and reparameterizing it
        (which will raise an error if this is not possible).
        """
        return self.reparam(self.sample_noise(size))

    def log_prob_zi(self, i, z):
        """log q(z_i | lambda_i)"""
        raise NotImplementedError()

class MFMixGaussian:
    """
    q(z | lambda ) = Dirichlet(z | lambda1) * Gaussian(z | lambda2) * Inv_Gamma(z|lambda3)
    """
    def __init__(self, D, K):
        self.K = K
        self.dirich = MFDirichlet(K, K)
        self.gauss = MFGaussian(K*D)
        self.invgam = MFInvGamma(K*D)

        dirich_num_vars = self.dirich.num_vars
        gauss_num_vars = self.gauss.num_vars
        invgam_num_vars = self.invgam.num_vars
        self.num_vars = dirich_num_vars + gauss_num_vars + invgam_num_vars

        dirich_num_param = self.dirich.num_params
        gauss_num_param = self.gauss.num_params
        invgam_num_params = self.invgam.num_params
        self.num_params = dirich_num_param + gauss_num_param + invgam_num_params

    def print_params(self, sess):
    	self.dirich.print_params(sess)
        self.gauss.print_params(sess)
        self.invgam.print_params(sess)

    def sample(self, size, sess):
        """z ~ q(z | lambda)"""

        dirich_samples = self.dirich.sample((size[0],self.dirich.num_vars), sess)
        gauss_samples = self.gauss.sample((size[0], self.gauss.num_vars), sess)
        invgam_samples = self.invgam.sample((size[0], self.invgam.num_vars), sess)

        z = np.concatenate((dirich_samples[0][0], gauss_samples, invgam_samples[0]), axis=0)

        return z.reshape(size)

    def log_prob_zi(self, i, z):
        """log q(z_i | lambda_i)"""

        log_prob = 0
        if i < self.dirich.num_vars:
            log_prob += self.dirich.log_prob_zi(i, z)

        if i < self.gauss.num_vars:
            log_prob += self.gauss.log_prob_zi(i, z)

        if i < self.invgam.num_vars:
            log_prob += self.invgam.log_prob_zi(i, z)

        if i >= self.num_vars:
            raise

        return log_prob

class MFDirichlet:
    """
    q(z | lambda ) = prod_{i=1}^d Dirichlet(z[i] | lambda[i])
    """
    def __init__(self, num_vars, K):
        self.K = K
        self.num_vars = num_vars
        self.num_params = K * num_vars
        self.alpha_unconst = tf.Variable(tf.random_normal([num_vars, K]))
        self.transform = tf.nn.softplus

    def print_params(self, sess):
        alpha = sess.run([self.transform(self.alpha_unconst)])

        print("concentration vector:")
        print(alpha)

    def sample(self, size, sess):
        """z ~ q(z | lambda)"""
        alpha = sess.run(self.transform(self.alpha_unconst))
        z = np.zeros((size[1], size[0], self.K))
        for d in xrange(self.num_vars):
            z[d, :, :] = dirichlet.rvs(alpha[d, :], size = size[0])

        return z

    def log_prob_zi(self, i, z):
        """log q(z_i | lambda_i)"""
        if i >= self.num_vars:
            raise

        alphai = self.transform(self.alpha_unconst)[i, :]

        return dirichlet.logpdf(z[:, i], alphai)

class MFInvGamma:
    """
    q(z | lambda ) = prod_{i=1}^d Inv_Gamma(z[i] | lambda[i])
    """
    def __init__(self, num_vars):
        self.num_vars = num_vars
        self.num_params = 2 * num_vars
        self.a_unconst = tf.Variable(tf.random_normal([num_vars]))
        self.b_unconst = tf.Variable(tf.random_normal([num_vars]))
        self.transform = tf.nn.softplus

    def print_params(self, sess):
        a, b = sess.run([ \
            self.transform(self.a_unconst),
            self.transform(self.b_unconst)])

        print("shape:")
        print(a)
        print("scale:")
        print(b)

    def sample(self, size, sess):
        """z ~ q(z | lambda)"""
        a, b = sess.run([ \
            self.transform(self.a_unconst),
            self.transform(self.b_unconst)])

        z = np.zeros(size)
        for d in range(self.num_vars):
            z[:, d] = invgamma.rvs(a[d], b[d], size=size[0])

        return z

    def log_prob_zi(self, i, z):
        """log q(z_i | lambda_i)"""
        if i >= self.num_vars:
            raise

        ai = self.transform(self.a_unconst)[i]
        bi = self.transform(self.b_unconst)[i]

        return invgamma.logpdf(z[:, i], ai, bi)

class MFBernoulli(Likelihood):
    """
    q(z | lambda ) = prod_{i=1}^d Bernoulli(z[i] | lambda[i])
    """
    def __init__(self, *args, **kwargs):
        Likelihood.__init__(self, *args, **kwargs)
        self.num_params = self.num_vars
        self.p = None

    def mapping(self, x):
        p = Variable("p", [self.num_vars])
        return [tf.sigmoid(p)]

    def set_params(self, params):
        self.p = params[0]
        # TODO constrain the parameters in simplex within mapping()
        d = get_dims(self.p)[0]
        if get_dims(self.p)[0] > 1:
            # TensorFlow supports neither negative indexing or assignment.
            #self.p[-1] = 1.0 - tf.reduce_sum(self.p[-1])
            self.p = concat([self.p[:(d-1)],
                             tf.expand_dims(1.0 - tf.reduce_sum(self.p[:(d-1)]), 0)])

    def print_params(self, sess):
        p = sess.run(self.p)
        print("probability:")
        print(p)

    def sample(self, size, sess):
        """z ~ q(z | lambda)"""
        p = sess.run(self.p)
        z = np.zeros(size)
        for d in range(self.num_vars):
            z[:, d] = bernoulli.rvs(p[d], size=size[0])

        return z

    def log_prob_zi(self, i, z):
        """log q(z_i | lambda_i)"""
        if i >= self.num_vars:
            raise

        return bernoulli.logpmf(z[:, i], self.p[i])

class MFBeta(Likelihood):
    """
    q(z | lambda ) = prod_{i=1}^d Beta(z[i] | lambda[i])
    """
    def __init__(self, *args, **kwargs):
        Likelihood.__init__(self, *args, **kwargs)
        self.num_params = 2*self.num_vars
        self.a = None
        self.b = None

    def mapping(self, x):
        alpha = Variable("alpha", [self.num_vars])
        beta = Variable("beta", [self.num_vars])
        return [tf.nn.softplus(alpha), tf.nn.softplus(beta)]

    def set_params(self, params):
        self.a = params[0]
        self.b = params[1]

    def print_params(self, sess):
        a, b = sess.run([self.a, self.b])
        print("shape:")
        print(a)
        print("scale:")
        print(b)

    def sample(self, size, sess):
        """z ~ q(z | lambda)"""
        a, b = sess.run([self.a, self.b])
        z = np.zeros(size)
        for d in range(self.num_vars):
            z[:, d] = beta.rvs(a[d], b[d], size=size[0])

        return z

    def log_prob_zi(self, i, z):
        """log q(z_i | lambda_i)"""
        if i >= self.num_vars:
            raise

        return beta.logpdf(z[:, i], self.a[i], self.b[i])

class MFGaussian(Likelihood):
    """
    q(z | lambda ) = prod_{i=1}^d Gaussian(z[i] | lambda[i])
    """
    def __init__(self, *args, **kwargs):
        Likelihood.__init__(self, *args, **kwargs)
        self.num_params = 2*self.num_vars
        self.m = None
        self.s = None

    def mapping(self, x):
        mean = Variable("mu", [self.num_vars])
        stddev = Variable("sigma", [self.num_vars])
        return [tf.identity(mean), tf.nn.softplus(stddev)]

    def set_params(self, params):
        self.m = params[0]
        self.s = params[1]

    def print_params(self, sess):
        m, s = sess.run([self.m, self.s])
        print("mean:")
        print(m)
        print("std dev:")
        print(s)

    def sample_noise(self, size):
        """
        eps = sample_noise() ~ s(eps)
        s.t. z = reparam(eps; lambda) ~ q(z | lambda)
        """
        # Not using this, since TensorFlow has a large overhead
        # whenever calling sess.run().
        #samples = sess.run(tf.random_normal(self.samples.get_shape()))
        return norm.rvs(size=size)

    def reparam(self, eps):
        """
        eps = sample_noise() ~ s(eps)
        s.t. z = reparam(eps; lambda) ~ q(z | lambda)
        """
        return self.m + eps * self.s

    def log_prob_zi(self, i, z):
        """log q(z_i | lambda_i)"""
        if i >= self.num_vars:
            raise

        mi = self.m[i]
        si = self.s[i]
        return concat([norm.logpdf(zm[i], mi, si)
                       for zm in tf.unpack(z)])

    # TODO entropy is bugged
    #def entropy(self):
    #    return norm.entropy(self.transform_s(self.s_unconst))

class PointMass(Likelihood):
    """
    Point mass variational family
    """
    def __init__(self, num_vars, transform=tf.identity):
        Likelihood.__init__(self, num_vars)
        self.num_params = self.num_vars
        self.transform = transform
        self.params = None

    def mapping(self, x):
        params = Variable("params", [self.num_vars])
        return [self.transform(params)]

    def set_params(self, params):
        self.params = params[0]

    def print_params(self, sess):
        params = sess.run(self.params)
        print("parameter values:")
        print(params)

    def get_params(self):
        return self.params
