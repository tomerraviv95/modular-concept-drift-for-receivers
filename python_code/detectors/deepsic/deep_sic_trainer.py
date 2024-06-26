from typing import List

import numpy as np
import torch
from torch import nn

from python_code import DEVICE
from python_code.channel.channels_hyperparams import N_ANT, N_USER
from python_code.channel.modulator import BPSKModulator
from python_code.detectors.deepsic.deep_sic_detector import DeepSICDetector
from python_code.detectors.trainer import Trainer
from python_code.drift_mechanisms.drift_mechanism_wrapper import TRAINING_TYPES
from python_code.utils.config_singleton import Config
from python_code.utils.constants import HALF
from python_code.utils.hotelling_test_utils import run_hotelling_test

conf = Config()
ITERATIONS = 3
EPOCHS = 250


def prob_to_BPSK_symbol(p: torch.Tensor) -> torch.Tensor:
    """
    prob_to_symbol(x:PyTorch/Numpy Tensor/Array)
    Converts Probabilities to BPSK Symbols by hard threshold: [0,0.5] -> '-1', [0.5,1] -> '+1'
    :param p: probabilities vector
    :return: symbols vector
    """
    return torch.sign(p - HALF)


class DeepSICTrainer(Trainer):
    """Form the trainer class.

    Keyword arguments:

    """

    def __init__(self):
        self.memory_length = 1
        self.n_user = N_USER
        self.n_ant = N_ANT
        self.lr = 1e-3
        self.ht = [0] * N_USER
        self.prev_ht_s1 = [[[] for _ in range(ITERATIONS)] for _ in range(self.n_user)]
        self.prev_ht_s0 = [[[] for _ in range(ITERATIONS)] for _ in range(self.n_user)]
        super().__init__()

    def __str__(self):
        name = 'DeepSIC'
        if conf.mechanism == TRAINING_TYPES.DRIFT.name and conf.modular:
            name = 'Modular ' + name
        return name

    def init_priors(self):
        self.probs_vec = HALF * torch.ones(conf.block_length - conf.pilot_size, N_ANT).to(DEVICE).float()
        self.pilots_probs_vec = HALF * torch.ones(conf.pilot_size, N_ANT).to(DEVICE).float()

    def _initialize_detector(self):
        self.detector = [[DeepSICDetector().to(DEVICE) for _ in range(ITERATIONS)] for _ in
                         range(self.n_user)]  # 2D list for Storing the DeepSIC Networks

    def calc_loss(self, est: torch.Tensor, tx: torch.IntTensor) -> torch.Tensor:
        """
        Cross Entropy loss - distribution over states versus the gt state label
        """
        return self.criterion(input=est, target=tx.long())

    @staticmethod
    def preprocess(rx: torch.Tensor) -> torch.Tensor:
        return rx.float()

    def train_model(self, single_model: nn.Module, tx: torch.Tensor, rx: torch.Tensor):
        """
        Trains a DeepSIC Network
        """
        self.optimizer = torch.optim.Adam(single_model.parameters(), lr=self.lr)
        self.criterion = torch.nn.CrossEntropyLoss()
        single_model = single_model.to(DEVICE)
        loss = 0
        y_total = self.preprocess(rx)
        for _ in range(EPOCHS):
            soft_estimation = single_model(y_total)
            current_loss = self.run_train_loop(soft_estimation, tx)
            loss += current_loss

    def train_models(self, model: List[List[DeepSICDetector]], i: int, tx_all: List[torch.Tensor],
                     rx_all: List[torch.Tensor]):

        for user in range(self.n_user):
            if not self.train_users_list[user]:
                continue
            self.train_model(model[user][i], tx_all[user], rx_all[user])

    def _online_training(self, tx: torch.Tensor, rx: torch.Tensor):
        """
        Main training function for DeepSIC trainer. Initializes the probabilities, then propagates them through the
        network, training sequentially each network and not by end-to-end manner (each one individually).
        """
        if conf.mechanism == TRAINING_TYPES.DRIFT:
            self._initialize_detector()
        initial_probs = tx.clone()
        tx_all, rx_all = self.prepare_data_for_training(tx, rx, initial_probs)
        # Training the DeepSIC network for each user for iteration=1
        self.train_models(self.detector, 0, tx_all, rx_all)
        # Initializing the probabilities
        probs_vec = HALF * torch.ones(tx.shape).to(DEVICE)
        # Training the DeepSICNet for each user-symbol/iteration
        for i in range(1, ITERATIONS):
            # Generating soft symbols for training purposes
            probs_vec = self.calculate_posteriors(self.detector, i, probs_vec, rx)
            # Obtaining the DeepSIC networks for each user-symbol and the i-th iteration
            tx_all, rx_all = self.prepare_data_for_training(tx, rx, probs_vec)
            # Training the DeepSIC networks for the iteration>1
            self.train_models(self.detector, i, tx_all, rx_all)

    def forward(self, rx: torch.Tensor) -> torch.Tensor:
        # detect and decode
        self.init_priors()
        for i in range(ITERATIONS):
            self.probs_vec = self.calculate_posteriors(self.detector, i + 1, self.probs_vec, rx)
        detected_word = BPSKModulator.demodulate(prob_to_BPSK_symbol(self.probs_vec.float()))
        return detected_word

    def forward_pilot(self, rx: torch.Tensor, tx: torch.Tensor) -> torch.Tensor:
        self.init_priors()
        # detect and decode
        ht_s0_t_0 = [[[] for _ in range(ITERATIONS)] for _ in range(self.n_user)]
        ht_s1_t_0 = [[[] for _ in range(ITERATIONS)] for _ in range(self.n_user)]
        ht_mat = [[[] for _ in range(ITERATIONS)] for _ in range(self.n_user)]
        for i in range(ITERATIONS):
            self.pilots_probs_vec = self.calculate_posteriors(self.detector, i + 1, self.pilots_probs_vec, rx)
            for user in range(self.n_user):
                rx_s0_idx = [i for i, x in enumerate(tx[:, user]) if x == 0]
                rx_s1_idx = [i for i, x in enumerate(tx[:, user]) if x == 1]
                # HT
                ht_s0_t_0[user][i] = self.pilots_probs_vec[rx_s0_idx, user].cpu().numpy()
                ht_s1_t_0[user][i] = self.pilots_probs_vec[rx_s1_idx, user].cpu().numpy()
                if np.shape(self.prev_ht_s0[user][i])[0] != 0:
                    run_hotelling_test(ht_mat, ht_s0_t_0, ht_s1_t_0, self.prev_ht_s0, self.prev_ht_s1, i, tx, user)
                # save previous distribution
                self.prev_ht_s0[user][i] = ht_s0_t_0[user][i].copy()
                self.prev_ht_s1[user][i] = ht_s1_t_0[user][i].copy()
        if np.prod(np.shape(ht_mat[self.n_user - 1][ITERATIONS - 1])) != 0:
            self.ht = [row[ITERATIONS - 1] for row in ht_mat]
        detected_word = BPSKModulator.demodulate(prob_to_BPSK_symbol(self.pilots_probs_vec.float()))
        return detected_word, self.pilots_probs_vec

    def prepare_data_for_training(self, tx: torch.Tensor, rx: torch.Tensor, probs_vec: torch.Tensor) -> [
        torch.Tensor, torch.Tensor]:
        """
        Generates the data for each user
        """
        tx_all = []
        rx_all = []
        for k in range(self.n_user):
            idx = [user_i for user_i in range(self.n_user) if user_i != k]
            current_y_train = torch.cat((rx, probs_vec[:, idx].reshape(rx.shape[0], -1)), dim=1)
            tx_all.append(tx[:, k])
            rx_all.append(current_y_train)
        return tx_all, rx_all

    def calculate_posteriors(self, model: List[List[nn.Module]], i: int, probs_vec: torch.Tensor,
                             rx: torch.Tensor) -> torch.Tensor:
        """
        Propagates the probabilities through the learnt networks.
        """
        next_probs_vec = torch.zeros(probs_vec.shape).to(DEVICE)
        for user in range(self.n_user):
            idx = [user_i for user_i in range(self.n_user) if user_i != user]
            input = torch.cat((rx, probs_vec[:, idx].reshape(rx.shape[0], -1)), dim=1)
            preprocessed_input = self.preprocess(input)
            with torch.no_grad():
                output = self.softmax(model[user][i - 1](preprocessed_input))
            next_probs_vec[:, user] = output[:, 1:].reshape(next_probs_vec[:, user].shape)
        return next_probs_vec
