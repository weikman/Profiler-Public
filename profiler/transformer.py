from __future__ import division
from profiler.globalvar import *
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm
import pandas as pd
import numpy as np
import logging


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def compute_differences(attr, dtype, env, operators, left, right, embed):
    if dtype == CATEGORICAL:
        df, c = compute_differences_categorical(env, attr, operators, left, right)
    elif dtype == NUMERIC:
        df, c = compute_differences_numerical(env, attr, operators, left, right)
    elif dtype == DATE:
        df, c = compute_differences_date(env, attr, operators, left, right)
    elif dtype == TEXT:
        if env['embedtxt']:
            df, c = compute_differences_text(env, attr, operators, left, right, embed)
        else:
            df, c = compute_differences_categorical(env, attr, operators, left, right)
    else:
        df = None
    return df, c


def compute_differences_text(env, attr, operators, left, right, embed):
    if embed is None:
        raise Exception('ERROR while creating training data. Embedding model is none')
    # handle null
    mask = left[(left == env['null']) | (right == env['null'])].index.values

    df = pd.DataFrame()
    left = embed.get_embedding(left, attr=attr).squeeze()
    right = embed.get_embedding(right, attr=attr).squeeze()
    diff = 1 - np.sum(np.multiply(left, right), axis=1) / (np.sqrt(np.sum(np.square(left), axis=1)) *
                                               np.sqrt(np.sum(np.square(right), axis=1)))

    if env['continuous']:
        df["%s_eq" % attr] = diff / np.nanmax(diff)
    else:
        df["%s_eq" % attr] = ((diff / np.nanmax(diff)) <= env['tol'])*2-1
        if NEQ in operators:
            df["%s_neq" % attr] = - df["%s_eq" % attr]

    # handle null
    df.iloc[mask, :] = np.zeros((len(mask), df.shape[1]))
    return df, len(mask)


def compute_differences_categorical(env, attr, operators, left, right):
    df = pd.DataFrame()
    mask = left[(left == env['null']) | (right == env['null'])].index.values
    df["%s_eq" % attr] = np.equal(left, right)*2-1
    if NEQ in operators:
        df["%s_neq" % attr] = - df["%s_eq"%attr]

    # handle null
    df.iloc[mask, :] = np.zeros((len(mask), df.shape[1]))
    return df, len(mask)


def compute_differences_numerical(env, attr, operators, left, right):
    df = pd.DataFrame()
    diff = left - right
    if env['continuous']:
        df["%s_eq" % attr] = np.abs(diff)
        if GT in operators:
            gt = diff.copy()
            gt[diff <= 0] = 0
            df["%s_gt" % attr] = gt
        if LT in operators:
            lt = diff.copy()
            lt[diff >= 0] = 0
            df["%s_lt" % attr] = lt
    else:
        df["%s_eq" % attr] = ((np.abs(diff) / np.nanmax(np.abs(diff))) <= env['tol'])*2-1
        if NEQ in operators:
            df["%s_neq" % attr] = - df["%s_eq"%attr]
        if GT in operators:
            df["%s_gt" % attr] = (diff > 0)*1
        if LT in operators:
            df["%s_lt" % attr] = (diff < 0)*1

    # handle null
    mask = left[np.isnan(diff)].index.values
    df.iloc[mask, :] = np.zeros((len(mask), df.shape[1]))
    return df, len(mask)

def compute_differences_date(env, attr, operators, left, right):
    df = pd.DataFrame()
    diff = ((left.values - right.values) / np.timedelta64(1, 's')).astype('float')
    if env['continuous']:
        df["%s_eq" % attr] = np.abs(diff) / np.nanmax(np.abs(diff))
        if GT in operators:
            gt = diff.copy()
            gt[diff <= 0] = 0
            df["%s_gt" % attr] = gt
        if LT in operators:
            lt = diff.copy()
            lt[diff >= 0] = 0
            df["%s_lt" % attr] = lt
    else:
        df["%s_eq" % attr] = ((np.abs(diff) / np.nanmax(np.abs(diff))) <= env['tol'])*2-1
        if NEQ in operators:
            df["%s_neq" % attr] = - df["%s_eq"%attr]
        if GT in operators:
            df["%s_gt" % attr] = (diff > 0)*2-1
        if LT in operators:
            df["%s_lt" % attr] = (diff < 0)*2-1
    # handle null
    mask = left[np.isnan(diff)].index.values
    df.iloc[mask, :] = np.zeros((len(mask), df.shape[1]))
    return df, len(mask)


class TransformEngine(object):
    """
    Transform input data to generate training data
    """

    def __init__(self, env, ds):
        self.env = env
        self.ds = ds
        self.embed = None

    @staticmethod
    def check_singular(df):
        # check singular
        to_drop = [col for col in df if len(df[col].unique()) == 1]
        if len(to_drop) != 0:
            df.drop(to_drop, axis=1, inplace=True)
        return df, np.unique(list(map(lambda x: "_".join(x.split('_')[0:-1]), to_drop))).tolist()

    def estimate_sample_size(self):
        # n > 1/eps^2*logp*(s+p) -> n > 1/eps^2*logp*((p-1)^2/2+p) = 1/eps^2*logp*(p^2/2 + 1/2)
        p = np.sum([len(op) for op in self.ds.operators.values()])
        min_n = 1/np.square(self.env['eps'])*np.log(p)*(np.square(p)/2+0.5)
        multiplier = int(np.ceil(min_n / self.ds.df.shape[0]))
        logger.info("needs multiplier = %d to bound the error in inv cov estimation <= %.8f"%(multiplier, self.env['eps']))
        multiplier = min(max(1, multiplier), self.ds.df.shape[0]-1)
        self.env['eps'] = (np.sqrt(np.square(p-1)/2+p) / (multiplier*self.ds.df.shape[0]))
        logger.info("use multiplier = %d, and the bound is %.8f"%(multiplier, self.env['eps']))
        return multiplier

    def create_training_data(self, multiplier=None, embed=None):
        self.embed = embed

        # handle nulls
        self.ds.replace_null()
        if self.env['null_policy'] == SKIP:
            self.ds.df.dropna(how="any", axis=0, inplace=True)

        if multiplier is None:
            multiplier = self.estimate_sample_size()
        n = multiplier * self.ds.df.shape[0]

        logger.info("Draw Pairs")
        left, right = self.create_pair_data(multiplier=multiplier)
        logger.info("Computing Differences")
        if self.env['workers'] < 1:
            data_count = [compute_differences(attr, self.ds.dtypes[attr], self.env, self.ds.operators[attr], left[attr],
                                        right[attr], self.embed) for attr in self.ds.field]
        else:
            pool = ThreadPoolExecutor(self.env['workers'])
            data_count = list(pool.map(lambda attr: compute_differences(attr, self.ds.dtypes[attr], self.env,
                                                                  self.ds.operators[attr], left[attr],
                                                                  right[attr], self.embed), self.ds.field))
        data = pd.concat([attr[0] for attr in data_count], axis=1)

        # turn data into nonsingualr matrix by dropping columns
        data, drop_cols = TransformEngine.check_singular(data)

        # obtain count of nulls
        null_counts = np.sum([attr[1] for i, attr in enumerate(data_count) if self.ds.field[i] not in drop_cols])
        null_pb = null_counts / (data.shape[0] * (len(self.ds.field) - len(drop_cols)))
        logger.info("estimated missing data probability in training data is %.4f" % null_pb)

        return data, null_pb, n

    def create_pair_data(self, multiplier):
        multiplier = max(1, int(np.ceil(multiplier/self.ds.field.shape[0])))
        # shift and concate
        lefts = []
        rights = []
        for attr in tqdm(self.ds.field):
            base_table = self.ds.df.sort_values(by=attr)
            left = [base_table] * multiplier
            right = [base_table.iloc[list(range(i+1, base_table.shape[0])) + list(range(i+1)), :].reset_index(
                drop=True) for i in range(multiplier)]
            lefts.extend(left)
            rights.extend(right)
        lefts = pd.concat(lefts).reset_index(drop=True)
        rights = pd.concat(rights).reset_index(drop=True)
        logger.info("Number of training samples: %d"%lefts.shape[0])
        return lefts, rights
