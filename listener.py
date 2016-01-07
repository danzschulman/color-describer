import colorsys
import numpy as np
import theano.tensor as T
from lasagne.layers import InputLayer, DropoutLayer, DenseLayer, EmbeddingLayer, NonlinearityLayer
from lasagne.layers.recurrent import LSTMLayer, Gate
from lasagne.init import Constant
from lasagne.objectives import categorical_crossentropy
from lasagne.nonlinearities import softmax
from lasagne.updates import rmsprop

from bt.learner import Learner
from bt import timing, config
from neural import LasagneModel, SequenceVectorizer, ColorVectorizer

parser = config.get_options_parser()
parser.add_argument('--listener_cell_size', default=20)
parser.add_argument('--listener_forget_bias', default=20)
parser.add_argument('--listener_color_resolution', default=4)


class ListenerLearner(Learner):
    '''
    An LSTM-based listener (guesses colors from descriptions).
    '''

    def __init__(self):
        options = config.options()
        res = options.listener_color_resolution

        self.seq_vec = SequenceVectorizer()
        self.color_vec = ColorVectorizer((res, res, res))

    def train(self, training_instances):
        options = config.options()

        xs, y = self._data_to_arrays(training_instances)
        self._build_model()

        print('Training')
        losses = []
        timing.start_task('Iteration', options.train_iters)
        for iteration in range(1, options.train_iters):
            timing.progress(iteration)
            losses_iter = self.model.fit(xs, y, batch_size=128, num_epochs=options.train_epochs)
            losses.append(losses_iter.tolist())
        timing.end_task()
        config.dump(losses, 'losses.jsons', lines=True)

    def predict(self, eval_instances):
        return self.predict_and_score(eval_instances)[0]

    def score(self, eval_instances):
        return self.predict_and_score(eval_instances)[1]

    def predict_and_score(self, eval_instances):
        xs, y = self._data_to_arrays(eval_instances, test=True)

        print('Testing')
        probs = self.model.predict(xs)
        predict = self.color_vec.unvectorize_all(probs.argmax(axis=1))
        bucket_volume = (256.0 ** 3) / self.color_vec.num_types
        scores_arr = np.log(bucket_volume) - np.log(probs[np.arange(len(eval_instances)), y])
        scores = scores_arr.tolist()
        return predict, scores

    def _data_to_arrays(self, training_instances, test=False):
        if not test:
            self.seq_vec.add_all(['<s>'] + inst.input.split() + ['</s>']
                                 for inst in training_instances)

        sentences = []
        colors = []
        for i, inst in enumerate(training_instances):
            desc, (hue, sat, val) = inst.input.split(), inst.output
            color_0_1 = colorsys.hsv_to_rgb(hue / 360.0, sat / 100.0, val / 100.0)
            color = tuple(min(d * 256, 255) for d in color_0_1)
            s = ['<s>'] * (self.seq_vec.max_len - 1 - len(desc)) + desc
            s.append('</s>')
            print('%s -> %s' % (repr(s), repr(color)))
            sentences.append(s)
            colors.append(color)
        print('Num sequences: %d' % len(sentences))

        print('Vectorization')
        x = np.zeros((len(sentences), self.seq_vec.max_len), dtype=np.int32)
        y = np.zeros((len(sentences),), dtype=np.int32)
        for i, sentence in enumerate(sentences):
            x[i, :] = self.seq_vec.vectorize(sentence)
            y[i] = self.color_vec.vectorize(colors[i])

        return x, y

    def _build_model(self):
        options = config.options()

        input_var = T.imatrix('inputs')
        target_var = T.ivector('targets')

        l_in = InputLayer(shape=(None, self.seq_vec.max_len), input_var=input_var)
        l_in_embed = EmbeddingLayer(l_in, input_size=len(self.seq_vec.tokens),
                                    output_size=options.listener_cell_size)
        l_lstm1 = LSTMLayer(l_in_embed, num_units=options.listener_cell_size,
                            forgetgate=Gate(b=Constant(options.listener_forget_bias)))
        l_lstm1_drop = DropoutLayer(l_lstm1, p=0.2)
        l_lstm2 = LSTMLayer(l_lstm1_drop, num_units=options.listener_cell_size,
                            forgetgate=Gate(b=Constant(options.listener_forget_bias)))
        l_lstm2_drop = DropoutLayer(l_lstm2, p=0.2)

        l_hidden = DenseLayer(l_lstm2_drop, num_units=options.listener_cell_size, nonlinearity=None)
        l_hidden_drop = DropoutLayer(l_hidden, p=0.2)
        l_scores = DenseLayer(l_hidden_drop, num_units=self.color_vec.num_types, nonlinearity=None)
        l_out = NonlinearityLayer(l_scores, nonlinearity=softmax)

        self.model = LasagneModel(input_var, target_var, l_out,
                                  loss=categorical_crossentropy, optimizer=rmsprop)