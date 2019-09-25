import json
import os
from collections import Counter

import numpy
import tensorflow as tf
from tensorflow.python.keras import models
from tensorflow.python.keras.layers import Embedding, Bidirectional, LSTM
from tensorflow.python.keras.models import Sequential
from tensorflow.python.keras import layers

from ioflow.configure import read_configure
from ioflow.corpus import get_corpus_processor
from seq2annotation.input import generate_tagset, Lookuper, \
    index_table_from_file
from tf_crf_layer.layer import CRF
from tf_crf_layer.loss import crf_loss, ConditionalRandomFieldLoss
from tf_crf_layer.metrics import crf_accuracy, sequence_span_accuracy, SequenceCorrectness
from tokenizer_tools.tagset.converter.offset_to_biluo import offset_to_biluo

# tf.enable_eager_execution()


from seq2annotation import unrandom


config = read_configure()

corpus = get_corpus_processor(config)
corpus.prepare()
train_data_generator_func = corpus.get_generator_func(corpus.TRAIN)
eval_data_generator_func = corpus.get_generator_func(corpus.EVAL)

corpus_meta_data = corpus.get_meta_info()

raw_tag_data = corpus_meta_data['tags']
tags_data = generate_tagset(corpus_meta_data['tags'])

train_data = list(train_data_generator_func())
eval_data = list(eval_data_generator_func())

tag_lookuper = Lookuper({v: i for i, v in enumerate(tags_data)})

vocab_data_file = config.get("vocabulary_file")

if not vocab_data_file:
    # load built in vocabulary file
    vocab_data_file = os.path.join(os.path.dirname(__file__), '../data/unicode_char_list.txt')

vocabulary_lookuper = index_table_from_file(vocab_data_file)


def classification_report(y_true, y_pred, labels):
    """
    Similar to the one in sklearn.metrics,
    reports per classs recall, precision and F1 score
    """
    y_true = numpy.asarray(y_true).ravel()
    y_pred = numpy.asarray(y_pred).ravel()
    corrects = Counter(yt for yt, yp in zip(y_true, y_pred) if yt == yp)
    y_true_counts = Counter(y_true)
    y_pred_counts = Counter(y_pred)
    report = ((lab,  # label
               corrects[i] / max(1, y_true_counts[i]),  # recall
               corrects[i] / max(1, y_pred_counts[i]),  # precision
               y_true_counts[i]  # support
               ) for i, lab in enumerate(labels))
    report = [(l, r, p, 2 * r * p / max(1e-9, r + p), s) for l, r, p, s in report]

    print('{:<15}{:>10}{:>10}{:>10}{:>10}\n'.format('',
                                                    'recall',
                                                    'precision',
                                                    'f1-score',
                                                    'support'))
    formatter = '{:<15}{:>10.2f}{:>10.2f}{:>10.2f}{:>10d}'.format
    for r in report:
        print(formatter(*r))
    print('')
    report2 = list(zip(*[(r * s, p * s, f1 * s) for l, r, p, f1, s in report]))
    N = len(y_true)
    print(formatter('avg / total',
                    sum(report2[0]) / N,
                    sum(report2[1]) / N,
                    sum(report2[2]) / N, N) + '\n')


def one_hot(a, num_classes):
    return numpy.squeeze(numpy.eye(num_classes)[a.reshape(-1)])


def preprocss(data, maxlen=None, intent_lookup_table=None):
    raw_x = []
    raw_y = []
    raw_intent = []

    for offset_data in data:
        tags = offset_to_biluo(offset_data)
        words = offset_data.text
        label = offset_data.extra_attr[config['intent_field']] if config['intent_field'] not in ["label"] else getattr(offset_data, config['intent_field'])

        tag_ids = [tag_lookuper.lookup(i) for i in tags]
        word_ids = [vocabulary_lookuper.lookup(i) for i in words]

        raw_x.append(word_ids)
        raw_y.append(tag_ids)
        raw_intent.append(label)

    if not intent_lookup_table:
        raw_intent_set = list(set(raw_intent))
        intent_lookup_table = Lookuper({v: i for i, v in enumerate(raw_intent_set)})

    intent_int_list = [intent_lookup_table.lookup(i) for i in raw_intent]

    if not maxlen:
        maxlen = max(len(s) for s in raw_x)

    x = tf.keras.preprocessing.sequence.pad_sequences(raw_x, maxlen,
                                                      padding='post')  # right padding

    # lef padded with -1. Indeed, any integer works as it will be masked
    # y_pos = pad_sequences(y_pos, maxlen, value=-1)
    # y_chunk = pad_sequences(y_chunk, maxlen, value=-1)
    y = tf.keras.preprocessing.sequence.pad_sequences(raw_y, maxlen, value=0,
                                                      padding='post')

    intent_np_array = numpy.array(intent_int_list)
    intent_one_hot = one_hot(intent_np_array, numpy.max(intent_np_array) + 1)

    return x, intent_one_hot, y, intent_lookup_table

MAX_LEN = 25

train_x, train_intent, train_y, intent_lookup_table = preprocss(train_data, MAX_LEN)
test_x, test_intent, test_y, _ = preprocss(eval_data, MAX_LEN, intent_lookup_table)

intent_number = train_intent.shape[1]

from tf_crf_layer.crf_dynamic_constraint_helper import generate_constraint_table, filter_constraint, sort_constraint

constraint_file = config.get("constraint")
with open(constraint_file) as fd:
    constraint = json.load(fd)

# filter out entity not in tag_list
tag_set = raw_tag_data
intent_set = intent_lookup_table.index_table.keys()
valid_constraint = filter_constraint(constraint, tag_set, intent_set)

wrong_constraint_mapping = list(valid_constraint.values())

right_constraint_mapping = sort_constraint(valid_constraint, intent_lookup_table.index_table)

tag_dict = tag_lookuper.inverse_index_table

expected_constraint_table = numpy.array([
    [
    #  Only allowed: Y entity for Y-domain
        #     O     B-X    B-Y    I-X    I-Y   L-X    L-Y    U-X    U-Y   start   end
        [     1,     0,     1,     0,     0,    0,     0,     0,     1,    0,     0],  # O
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # B-X
        [     0,     0,     0,     0,     1,    0,     1,     0,     0,    0,     0],  # B-Y
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # I-X
        [     0,     0,     0,     0,     1,    0,     1,     0,     0,    0,     0],  # I-Y
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # L-X
        [     1,     0,     1,     0,     0,    0,     0,     0,     1,    0,     0],  # L-Y
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # U-X
        [     1,     0,     1,     0,     0,    0,     0,     0,     1,    0,     0],  # U-Y
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # start
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # end
    ],
    [
    #  Only allowed: X entity for X-domain
        #     O     B-X    B-Y    I-X    I-Y   L-X    L-Y    U-X    U-Y   start   end
        [     1,     1,     0,     0,     0,    0,     0,     1,     0,    0,     0],  # O
        [     0,     0,     0,     1,     0,    1,     0,     0,     0,    0,     0],  # B-X
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # B-Y
        [     0,     0,     0,     1,     0,    1,     0,     0,     0,    0,     0],  # I-X
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # I-Y
        [     1,     1,     0,     0,     0,    0,     0,     1,     0,    0,     0],  # L-X
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # L-Y
        [     1,     1,     0,     0,     0,    0,     0,     1,     0,    0,     0],  # U-X
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # U-Y
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # start
        [     0,     0,     0,     0,     0,    0,     0,     0,     0,    0,     0],  # end
    ]
], dtype=numpy.bool)

constraint_table = generate_constraint_table(right_constraint_mapping, tag_dict)

# diff = numpy.bitwise_xor(constraint_table, expected_constraint_table)

EPOCHS = config['epochs']
EMBED_DIM = config['embedding_dim']
BiRNN_UNITS = config['lstm_size']

vacab_size = vocabulary_lookuper.size()
tag_size = tag_lookuper.size()

# model = Sequential()
# model.add(Embedding(vacab_size, EMBED_DIM, mask_zero=True))
# model.add(Bidirectional(LSTM(BiRNN_UNITS // 2, return_sequences=True)))
# model.add(CRF(tag_size))


raw_input = layers.Input(shape=(MAX_LEN,))
embedding_layer = Embedding(vacab_size, EMBED_DIM, mask_zero=True)(raw_input)
bilstm_layer = Bidirectional(LSTM(BiRNN_UNITS // 2, return_sequences=True))(embedding_layer)

crf_layer = CRF(
    units=tag_size,
    transition_constraint_matrix=constraint_table,
    name='crf'
)

dynamic_constraint_input = layers.Input(shape=(intent_number,))

output_layer = crf_layer([bilstm_layer, dynamic_constraint_input])

model = models.Model([raw_input, dynamic_constraint_input], output_layer)

# print model summary
model.summary()

callbacks_list = []

tensorboard_callback = tf.keras.callbacks.TensorBoard(log_dir=config['summary_log_dir'])
callbacks_list.append(tensorboard_callback)

checkpoint_callback = tf.keras.callbacks.ModelCheckpoint(
    os.path.join(config['model_dir'], 'cp-{epoch:04d}.ckpt'),
    load_weights_on_restart=True,
    verbose=1
)
callbacks_list.append(checkpoint_callback)

metrics_list = []

metrics_list.append(crf_accuracy)
metrics_list.append(SequenceCorrectness())
metrics_list.append(sequence_span_accuracy)

loss_func = ConditionalRandomFieldLoss()
# loss_func = crf_loss

model.compile('adam', loss={'crf': loss_func}, metrics=metrics_list)

model.fit(
    [train_x, train_intent], train_y,
    epochs=EPOCHS,
    validation_data=[[test_x, test_intent], test_y],
    callbacks=callbacks_list
)

# Save the model
model.save(config['h5_model_file'])
tag_lookuper.dump_to_file('./results/h5_model/tag_lookup_table.json')
vocabulary_lookuper.dump_to_file('./results/h5_model/vocabulary_lookup_table.json')
tf.keras.experimental.export_saved_model(model, config['saved_model_dir'])

