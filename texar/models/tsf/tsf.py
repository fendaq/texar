"""
Text Style Transfer.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import pdb

import copy
import numpy as np
import tensorflow as tf

from texar import context
from texar.hyperparams import HParams
from texar.core.utils import switch_dropout
from texar.modules.encoders.conv1d_discriminator import CNN
from texar.models.tsf import ops
from texar.models.tsf import utils


class TSF:
  """Text style transfer."""

  def __init__(self, hparams=None):
    self._hparams = HParams(hparams, self.default_hparams(),
                            allow_new_hparam=True)
    self.input_tensors = self._build_inputs()
    (self.output_tensors, self.loss, self.opt) \
      = self._build_model(self.input_tensors)
    self.saver = tf.train.Saver()

  @staticmethod
  def default_hparams():
    return {
      "name": "text_style_transfer",
      "collections": "tsf",
      "batch_size": 128,
      "embedding_size": 100,
      # rnn hprams
      "rnn_type": "GRUCell",
      "rnn_size": 700,
      "rnn_input_keep_prob": 0.5,
      "output_keep_prob": 0.5,
      "dim_y": 200,
      "dim_z": 500,
      # cnn
      "cnn_name": "cnn",
      "cnn_kernel_sizes": [3, 4, 5],
      "cnn_num_filter": 128,
      "cnn_input_keep_prob": 1.,
      "cnn_output_keep_prob": 0.5,
      # adam
      "adam_learning_rate": 1e-4,
      "adam_beta1": 0.9,
      "adam_beta2": 0.999
    }

  def _build_inputs(self):
    batch_size = self._hparams.batch_size

    enc_inputs = tf.placeholder(tf.int32, [batch_size, None], name="enc_inputs")
    dec_inputs = tf.placeholder(tf.int32, [batch_size, None], name="dec_inputs")
    targets = tf.placeholder(tf.int32, [batch_size, None], name="targets")
    weights = tf.placeholder(tf.float32, [batch_size, None], name="weights")
    labels = tf.placeholder(tf.float32, [batch_size], name="labels")
    batch_len = tf.placeholder(tf.int32, name="batch_len")
    gamma = tf.placeholder(tf.float32, name="gamma")
    rho = tf.placeholder(tf.float32, name="rho")

    collections_input = self._hparams.collections + '/input'
    input_tensors = utils.register_collection(
      collections_input,
      [("enc_inputs", enc_inputs),
       ("dec_inputs", dec_inputs),
       ("targets", targets),
       ("weights", weights),
       ("labels", labels),
       ("batch_len", batch_len),
       ("gamma", gamma),
       ("rho", rho),
      ])

    return input_tensors

  def _build_model(self, input_tensors, reuse=False):
    hparams = self._hparams
    embedding_init = np.random.random_sample(
      (hparams.vocab_size, hparams.embedding_size)) - 0.5
    for i in range(hparams.vocab_size):
      embedding_init[i] /= np.linalg.norm(embedding_init[i])
    # embedding = tf.get_variable(
    #   "embedding", shape=[hparams.vocab_size, hparams.embedding_size])
    embedding = tf.get_variable(
      "embedding", initializer=embedding_init.astype(np.float32))

    enc_inputs = tf.nn.embedding_lookup(embedding, input_tensors["enc_inputs"])
    dec_inputs = tf.nn.embedding_lookup(embedding, input_tensors["dec_inputs"])

    labels = input_tensors["labels"]
    labels = tf.reshape(labels, [-1, 1])

    # auto encoder
    # label_proj_e = tf.layers.Dense(hparams.dim_y, name="encoder")
    # init_state = tf.concat([label_proj_e(labels),
    #                         tf.zeros([hparams.batch_size, hparams.dim_z])], 1)

    rnn_hparams = utils.filter_hparams(hparams, "rnn")
    init_state = tf.zeros([hparams.batch_size, rnn_hparams.size])
    cell_e = ops.get_rnn_cell(rnn_hparams)

    _, z = tf.nn.dynamic_rnn(cell_e, enc_inputs, initial_state=init_state,
                              scope="encoder")
    z  = z[:, hparams.dim_y:]

    label_proj_g = tf.layers.Dense(hparams.dim_y, name="generator")
    h_ori = tf.concat([label_proj_g(labels), z], 1)
    h_tsf = tf.concat([label_proj_g(1 - labels), z], 1)

    cell_g = ops.get_rnn_cell(rnn_hparams)
    softmax_proj = tf.layers.Dense(hparams.vocab_size, name="softmax_proj")
    g_outputs, _ = tf.nn.dynamic_rnn(cell_g, dec_inputs, initial_state=h_ori,
                                     scope="generator")

    teach_h = tf.concat([tf.expand_dims(h_ori, 1), g_outputs], 1)

    g_outputs = tf.nn.dropout(
      g_outputs, switch_dropout(hparams.output_keep_prob))
    g_logits = softmax_proj(tf.reshape(g_outputs, [-1, rnn_hparams.size]))

    loss_g = tf.nn.sparse_softmax_cross_entropy_with_logits(
      labels=tf.reshape(input_tensors["targets"], [-1]), logits=g_logits)
    loss_g *= tf.reshape(input_tensors["weights"], [-1])
    ppl_g = tf.reduce_sum(loss_g) / (tf.reduce_sum(input_tensors["weights"]) \
                                     + 1e-8)
    loss_g = tf.reduce_sum(loss_g) / hparams.batch_size
    # decoding 
    go = dec_inputs[:, 0, :]
    #  soft_func = feed_softmax(softmax_proj, embedding, input_tensors["gamma"])
    soft_func = ops.sample_gumbel(softmax_proj, embedding,
                                  input_tensors["gamma"],
                                  output_keep_prob=hparams.output_keep_prob)
    hard_func = ops.greedy_softmax(softmax_proj, embedding,
                                   output_keep_prob=hparams.output_keep_prob)

    soft_output_ori, soft_logits_ori, _ = ops.rnn_decode(
      h_ori, go, hparams.max_len, cell_g, soft_func, scope="generator")
    soft_output_tsf, soft_logits_tsf, _ = ops.rnn_decode(
      h_tsf, go, hparams.max_len, cell_g, soft_func, scope="generator")

    hard_output_ori, hard_logits_ori, _ = ops.rnn_decode(
      h_ori, go, hparams.max_len, cell_g, hard_func, scope="generator")
    hard_output_tsf, hard_logits_tsf, _ = ops.rnn_decode(
      h_tsf, go, hparams.max_len, cell_g, hard_func, scope="generator")

    with tf.variable_scope("generator", reuse=True):
      test_output, _ = cell_g(go, h_ori)
    test_logits = softmax_proj(test_output)

    # discriminator
    half = hparams.batch_size // 2
    # plus the encoder h
    soft_output_tsf = soft_output_tsf[:, :input_tensors["batch_len"], :]
    soft_h_tsf = tf.concat([tf.expand_dims(h_tsf, 1), soft_output_tsf], 1)

    cnn_hparams = utils.filter_hparams(hparams, "cnn")
    cnn0_hparams = copy.deepcopy(cnn_hparams)
    cnn1_hparams = copy.deepcopy(cnn_hparams)
    cnn0_hparams.name = "cnn0"
    cnn1_hparams.name = "cnn1"
    
    cnn0 = CNN(cnn0_hparams)
    cnn1 = CNN(cnn1_hparams)

    loss_d0 = ops.adv_loss(teach_h[:half], soft_h_tsf[half:], cnn0)
    loss_d1 = ops.adv_loss(teach_h[half:], soft_h_tsf[:half], cnn1)

    loss_d = loss_d0 + loss_d1
    loss =loss_g - input_tensors["rho"] * loss_d

    var_eg = ops.retrieve_variables(["encoder", "generator",
                                     "softmax_proj", "embedding"])
    var_d0 = ops.retrieve_variables(["cnn0"])
    var_d1 = ops.retrieve_variables(["cnn1"])

    # optimization
    adam_hparams = utils.filter_hparams(hparams, "adam")
    optimizer_all = tf.train.AdamOptimizer(**adam_hparams).minimize(
      loss, var_list=var_eg)
    optimizer_ae = tf.train.AdamOptimizer(**adam_hparams).minimize(
      loss_g, var_list=var_eg)
    optimizer_d0 = tf.train.AdamOptimizer(**adam_hparams).minimize(
      loss_d0, var_list=var_d0)
    optimizer_d1 = tf.train.AdamOptimizer(**adam_hparams).minimize(
      loss_d1, var_list=var_d1)

    # add tensors to collections
    collections_output = hparams.collections + '/output'
    output_tensors = utils.register_collection(
      collections_output,
      [("h_ori", h_ori),
       ("h_tsf", h_tsf),
       ("hard_logits_ori", hard_logits_ori),
       ("hard_logits_tsf", hard_logits_tsf),
       ("soft_logits_ori", soft_logits_ori),
       ("soft_logits_tsf", soft_logits_tsf),
       ("g_logits", g_logits),
       ("test_output", test_output),
       ("test_logits", test_logits),
       ("teach_h", teach_h),
       ("soft_h_tsf", soft_h_tsf),
      ]
    )

    collections_loss = hparams.collections + '/loss'
    loss = utils.register_collection(
      collections_loss,
      [("loss", loss),
       ("loss_g", loss_g),
       ("ppl_g", ppl_g),
       ("loss_d", loss_d),
       ("loss_d0", loss_d0),
       ("loss_d1", loss_d1),
      ]
    )

    collections_opt = hparams.collections + '/opt'
    opt = utils.register_collection(
      collections_opt,
      [("optimizer_all", optimizer_all),
       ("optimizer_ae", optimizer_ae),
       ("optimizer_d0", optimizer_d0),
       ("optimizer_d1", optimizer_d1),
      ]
    )

    return output_tensors, loss, opt

  def train_d0_step(self, sess, batch, rho, gamma):
    loss_d0, _ = sess.run(
      [self.loss["loss_d0"], self.opt["optimizer_d0"],],
      self.feed_dict(batch, rho, gamma))
    return loss_d0

  def train_d1_step(self, sess, batch, rho, gamma):
    loss_d1, _ = sess.run([self.loss["loss_d1"], self.opt["optimizer_d1"]],
                          self.feed_dict(batch, rho, gamma))
    return loss_d1

  def train_g_step(self, sess, batch, rho, gamma):
    loss, loss_g, ppl_g, loss_d, _ = sess.run(
      [self.loss["loss"],
       self.loss["loss_g"],
       self.loss["ppl_g"],
       self.loss["loss_d"],
       self.opt["optimizer_all"]],
      self.feed_dict(batch, rho, gamma))
    return loss, loss_g, ppl_g, loss_d

  def train_ae_step(self, sess, batch, rho, gamma):
    loss, loss_g, ppl_g, loss_d, _ = sess.run(
      [self.loss["loss"],
       self.loss["loss_g"],
       self.loss["ppl_g"],
       self.loss["loss_d"],
       self.opt["optimizer_ae"]],
      self.feed_dict(batch, rho, gamma))
    return loss, loss_g, ppl_g, loss_d

  def eval_step(self, sess, batch, rho, gamma):
    loss, loss_g, ppl_g, loss_d, loss_d0, loss_d1 = sess.run(
      [self.loss["loss"],
       self.loss["loss_g"],
       self.loss["ppl_g"],
       self.loss["loss_d"],
       self.loss["loss_d0"],
       self.loss["loss_d1"]],
      self.feed_dict(batch, rho, gamma, is_train=False))
    return loss, loss_g, ppl_g, loss_d, loss_d0, loss_d1

  def decode_step(self, sess, batch):
    logits_ori, logits_tsf = sess.run(
      [self.output_tensors["hard_logits_ori"],
       self.output_tensors["hard_logits_tsf"]],
      feed_dict={
        context.is_train(): False,
        self.input_tensors["enc_inputs"]: batch["enc_inputs"],
        self.input_tensors["dec_inputs"]: batch["dec_inputs"],
        self.input_tensors["labels"]: batch["labels"]})
    return logits_ori, logits_tsf

  def feed_dict(self, batch, rho, gamma, is_train=True):
    return {
      context.is_train(): is_train,
      self.input_tensors["batch_len"]: batch["len"],
      self.input_tensors["enc_inputs"]: batch["enc_inputs"],
      self.input_tensors["dec_inputs"]: batch["dec_inputs"],
      self.input_tensors["targets"]: batch["targets"],
      self.input_tensors["weights"]: batch["weights"],
      self.input_tensors["labels"]: batch["labels"],
      self.input_tensors["rho"]: rho,
      self.input_tensors["gamma"]: gamma,
    }

  def decode_step_soft(self, sess, batch, gamma=0.01):
    logits_ori, logits_tsf, g_logits, test_output, test_logits = sess.run(
      [self.output_tensors["soft_logits_ori"],
       self.output_tensors["soft_logits_tsf"],
       self.output_tensors["g_logits"],
       self.output_tensors["test_output"],
       self.output_tensors["test_logits"]],
      feed_dict={
        context.is_train(): False,
        self.input_tensors["enc_inputs"]: batch["enc_inputs"],
        self.input_tensors["dec_inputs"]: batch["dec_inputs"],
        self.input_tensors["labels"]: batch["labels"],
        self.input_tensors["gamma"]: gamma})
    return logits_ori, logits_tsf, g_logits, test_output, test_logits
