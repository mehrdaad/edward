from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import six
import tensorflow as tf

from edward.inferences.inference import (check_and_maybe_build_data,
    check_and_maybe_build_latent_vars, transform, check_and_maybe_build_dict, check_and_maybe_build_var_list)
from edward.models import RandomVariable
from edward.util import copy, get_descendants


def wake_sleep(latent_vars=None, data=None, n_samples=1, phase_q='sleep',
               auto_transform=True, scale=None, var_list=None, collections=None):
  """Wake-Sleep algorithm [@hinton1995wake].

  Given a probability model $p(x, z; \\theta)$ and variational
  distribution $q(z\mid x; \\lambda)$, wake-sleep alternates between
  two phases:

  + In the wake phase, $\log p(x, z; \\theta)$ is maximized with
  respect to model parameters $\\theta$ using bottom-up samples
  $z\sim q(z\mid x; \lambda)$.
  + In the sleep phase, $\log q(z\mid x; \lambda)$ is maximized with
  respect to variational parameters $\lambda$ using top-down
  "fantasy" samples $z\sim p(x, z; \\theta)$.

  @hinton1995wake justify wake-sleep under the variational lower
  bound of the description length,

  $\mathbb{E}_{q(z\mid x; \lambda)} [
      \log p(x, z; \\theta) - \log q(z\mid x; \lambda)].$

  Maximizing it with respect to $\\theta$ corresponds to the wake phase.
  Instead of maximizing it with respect to $\lambda$ (which
  corresponds to minimizing $\\text{KL}(q\|p)$), the sleep phase
  corresponds to minimizing the reverse KL $\\text{KL}(p\|q)$ in
  expectation over the data distribution.

  #### Notes

  In conditional inference, we infer $z$ in $p(z, \\beta
  \mid x)$ while fixing inference over $\\beta$ using another
  distribution $q(\\beta)$. During gradient calculation, instead
  of using the model's density

  $\log p(x, z^{(s)}), z^{(s)} \sim q(z; \lambda),$

  for each sample $s=1,\ldots,S$, `WakeSleep` uses

  $\log p(x, z^{(s)}, \\beta^{(s)}),$

  where $z^{(s)} \sim q(z; \lambda)$ and $\\beta^{(s)}
  \sim q(\\beta)$.

  The objective function also adds to itself a summation over all
  tensors in the `REGULARIZATION_LOSSES` collection.
  """
  """
  Args:
    n_samples: int, optional.
      Number of samples for calculating stochastic gradients during
      wake and sleep phases.
    phase_q: str, optional.
      Phase for updating parameters of q. If 'sleep', update using
      a sample from p. If 'wake', update using a sample from q.
      (Unlike reparameterization gradients, the sample is held
      fixed.)
  """
  latent_vars = check_and_maybe_build_latent_vars(latent_vars)
  data = check_and_maybe_build_data(data)
  latent_vars, _ = transform(latent_vars, auto_transform)
  scale = check_and_maybe_build_dict(scale)
  var_list = check_and_maybe_build_var_list(var_list, latent_vars, data)

  p_log_prob = [0.0] * n_samples
  q_log_prob = [0.0] * n_samples
  base_scope = tf.get_default_graph().unique_name("inference") + '/'
  for s in range(n_samples):
    # Form dictionary in order to replace conditioning on prior or
    # observed variable with conditioning on a specific value.
    scope = base_scope + tf.get_default_graph().unique_name("q_sample")
    dict_swap = {}
    for x, qx in six.iteritems(data):
      if isinstance(x, RandomVariable):
        if isinstance(qx, RandomVariable):
          qx_copy = copy(qx, scope=scope)
          dict_swap[x] = qx_copy.value
        else:
          dict_swap[x] = qx

    # Sample z ~ q(z), then compute log p(x, z).
    q_dict_swap = dict_swap.copy()
    for z, qz in six.iteritems(latent_vars):
      # Copy q(z) to obtain new set of posterior samples.
      qz_copy = copy(qz, scope=scope)
      q_dict_swap[z] = qz_copy.value
      if phase_q != 'sleep':
        # If not sleep phase, compute log q(z).
        q_log_prob[s] += tf.reduce_sum(
            scale.get(z, 1.0) *
            qz_copy.log_prob(tf.stop_gradient(q_dict_swap[z])))

    for z in six.iterkeys(latent_vars):
      z_copy = copy(z, q_dict_swap, scope=scope)
      p_log_prob[s] += tf.reduce_sum(
          scale.get(z, 1.0) * z_copy.log_prob(q_dict_swap[z]))

    for x in six.iterkeys(data):
      if isinstance(x, RandomVariable):
        x_copy = copy(x, q_dict_swap, scope=scope)
        p_log_prob[s] += tf.reduce_sum(
            scale.get(x, 1.0) * x_copy.log_prob(q_dict_swap[x]))

    if phase_q == 'sleep':
      # Sample z ~ p(z), then compute log q(z).
      scope = base_scope + tf.get_default_graph().unique_name("p_sample")
      p_dict_swap = dict_swap.copy()
      for z, qz in six.iteritems(latent_vars):
        # Copy p(z) to obtain new set of prior samples.
        z_copy = copy(z, scope=scope)
        p_dict_swap[qz] = z_copy.value
      for qz in six.itervalues(latent_vars):
        qz_copy = copy(qz, p_dict_swap, scope=scope)
        q_log_prob[s] += tf.reduce_sum(
            scale.get(z, 1.0) *
            qz_copy.log_prob(tf.stop_gradient(p_dict_swap[qz])))

  p_log_prob = tf.reduce_mean(p_log_prob)
  q_log_prob = tf.reduce_mean(q_log_prob)
  reg_penalty = tf.reduce_sum(tf.losses.get_regularization_losses())

  if collections is not None:
    tf.summary.scalar("loss/p_log_prob", p_log_prob,
                      collections=collections)
    tf.summary.scalar("loss/q_log_prob", q_log_prob,
                      collections=collections)
    tf.summary.scalar("loss/reg_penalty", reg_penalty,
                      collections=collections)

  loss_p = -p_log_prob + reg_penalty
  loss_q = -q_log_prob + reg_penalty

  q_rvs = list(six.itervalues(latent_vars))
  q_vars = [v for v in var_list
            if len(get_descendants(tf.convert_to_tensor(v), q_rvs)) != 0]
  q_grads = tf.gradients(loss_q, q_vars)
  p_vars = [v for v in var_list if v not in q_vars]
  p_grads = tf.gradients(loss_p, p_vars)
  grads_and_vars = list(zip(q_grads, q_vars)) + list(zip(p_grads, p_vars))
  return loss_p, grads_and_vars
