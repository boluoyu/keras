from ..engine import Layer, InputSpec
from .. import backend as K


class Wrapper(Layer):

    def __init__(self, layer, **kwargs):
        self.layer = layer
        self.uses_learning_phase = layer.uses_learning_phase
        super(Wrapper, self).__init__(**kwargs)

    def build(self, input_shape=None):
        '''Assumes that self.layer is already set.
        Should be called at the end of .build() in the
        children classes.
        '''
        self.trainable_weights = getattr(self.layer, 'trainable_weights', [])
        self.non_trainable_weights = getattr(self.layer, 'non_trainable_weights', [])
        self.updates = getattr(self.layer, 'updates', [])
        self.losses = getattr(self.layer, 'losses', [])
        self.constraints = getattr(self.layer, 'constraints', {})

        # properly attribute the current layer to
        # regularizers that need access to it
        # (e.g. ActivityRegularizer).
        #for regularizer in self.regularizers:
        #    if hasattr(regularizer, 'set_layer'):
        #        regularizer.set_layer(self)

    def get_weights(self):
        weights = self.layer.get_weights()
        return weights

    def set_weights(self, weights):
        self.layer.set_weights(weights)

    def get_config(self):
        config = {'layer': {'class_name': self.layer.__class__.__name__,
                            'config': self.layer.get_config()}}
        base_config = super(Wrapper, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    @classmethod
    def from_config(cls, config):
        from keras.utils.layer_utils import layer_from_config
        layer = layer_from_config(config.pop('layer'))
        return cls(layer, **config)


class TimeDistributed(Wrapper):
    """This wrapper allows to apply a layer to every
    temporal slice of an input.

    The input should be at least 3D,
    and the dimension of index one will be considered to be
    the temporal dimension.

    Consider a batch of 32 samples, where each sample is a sequence of 10
    vectors of 16 dimensions. The batch input shape of the layer is then `(32, 10, 16)`
    (and the `input_shape`, not including the samples dimension, is `(10, 16)`).

    You can then use `TimeDistributed` to apply a `Dense` layer to each of the 10 timesteps, independently:
    ```python
        # as the first layer in a model
        model = Sequential()
        model.add(TimeDistributed(Dense(8), input_shape=(10, 16)))
        # now model.output_shape == (None, 10, 8)

        # subsequent layers: no need for input_shape
        model.add(TimeDistributed(Dense(32)))
        # now model.output_shape == (None, 10, 32)
    ```

    The output will then have shape `(32, 10, 8)`.

    Note this is strictly equivalent to using `layers.core.TimeDistributedDense`.
    However what is different about `TimeDistributed`
    is that it can be used with arbitrary layers, not just `Dense`,
    for instance with a `Convolution2D` layer:

    ```python
        model = Sequential()
        model.add(TimeDistributed(Convolution2D(64, 3, 3), input_shape=(10, 3, 299, 299)))
    ```

    # Arguments
        layer: a layer instance.
    """
    def __init__(self, layer, **kwargs):
        self.supports_masking = True
        super(TimeDistributed, self).__init__(layer, **kwargs)

    def build(self, input_shape):
        #assert len(input_shape) >= 3
        #self.input_spec = [InputSpec(shape=input_shape)]

        if type(input_shape) != list:
            input_shape = [input_shape]
        for shape in input_shape:
            assert len(shape) >= 3
        self.input_spec = [InputSpec(shape=shape) for shape in input_shape]

        #child_input_shape = (input_shape[0],) + input_shape[2:]

        child_input_shape = [((shape[0],) + shape[2:]) for shape in input_shape]
        if len(input_shape) == 1:
            child_input_shape = child_input_shape[0]

        if not self.layer.built:
            self.layer.build(child_input_shape)
            self.layer.built = True
        super(TimeDistributed, self).build()

    @property
    def output_shape(self):
        child_output_shape = self.layer.output_shape
        timesteps = self.input_shape[1]
        return (child_output_shape[0], timesteps) + child_output_shape[1:]

    def get_output(self, train=False):
        X = self.get_input(train)
        mask = self.get_input_mask(train)

        input_shape = self.input_spec[0].shape
        self.input_spec = [InputSpec(shape=input_shape)]
        if K._BACKEND == 'tensorflow':
            if not input_shape[1]:
                raise Exception('When using TensorFlow, you should define '
                                'explicitly the number of timesteps of '
                                'your sequences.\n'
                                'If your first layer is an Embedding, '
                                'make sure to pass it an "input_length" '
                                'argument. Otherwise, make sure '
                                'the first layer has '
                                'an "input_shape" or "batch_input_shape" '
                                'argument, including the time axis.')
        child_input_shape = (input_shape[0],) + input_shape[2:]
        if not self.layer.built:
            self.layer.build(child_input_shape)
            self.layer.built = True
        super(TimeDistributed, self).build()

    def get_output_shape_for(self, input_shape):

        #child_input_shape = (input_shape[0],) + input_shape[2:]

        if type(input_shape) == list:
            all_timesteps = [shape[1] for shape in input_shape]
            if None in all_timesteps:
                timesteps = None
            else:
                timesteps = max(all_timesteps)
            child_input_shape = [((shape[0],) + shape[2:]) for shape in input_shape]
            #timesteps = input_shape[0][1]
        else:
            child_input_shape = (input_shape[0],) + input_shape[2:]
            timesteps = input_shape[1]

        child_output_shape = self.layer.get_output_shape_for(child_input_shape)

        #timesteps = input_shape[1]
        return (child_output_shape[0], timesteps) + child_output_shape[1:]

    def compute_mask(self, input, input_mask):
        if type(input_mask) != list:
            return input_mask
        elif not any(input_mask):
            return None
        else:
            not_None_masks = [m for m in input_mask if m is not None]
            if len(not_None_masks) == 1:
                return not_None_masks[0]
            else:
                input_mask = K.concatenate(input_mask)
                return K.prod(input_mask, axis=0)

    def call(self, X, mask=None):
        input_shape = K.int_shape(X)
        #if input_shape[0]:

        input_shapes = [input_spec.shape for input_spec in self.input_spec]
        batch_size = False
        for shape in input_shapes:
            if shape[0] is not None:
                batch_size = True
                break

        if batch_size:
            # batch size matters, use rnn-based implementation
            def step(x, states):
                output = self.layer.call(x)
                return output, []

            _, outputs, _ = K.rnn(step, X,
                                  initial_states=[],
                                  input_length=input_shape[1],
                                  unroll=False)
            y = outputs
        else:
            # no batch size specified, therefore the layer will be able
            # to process batches of any size
            # we can go with reshape-based implementation for performance

            if type(X) != list:
                X = [X]
            input_length = [K.shape(X[i])[1] for i in range(len(X))]
            max_length = K.max(input_length)

            # repeat broadcastable inputs for balancing dimensions
            X = [K.repeatRdim(X[i], max_length, axis=1) if input_length[i]==1 else X[i] for i in range(len(X))]
            # (nb_samples * timesteps, ...)
            X = [K.reshape(X[i], (-1,) + input_shapes[i][2:]) for i in range(len(X))]

            if len(X) == 1:
                X = X[0]
                input_shape = input_shapes[0]
            else:
                input_shape = input_shapes

            y = self.layer.call(X)  # (nb_samples * timesteps, ...)
            # (nb_samples, timesteps, ...)
            output_shape = self.get_output_shape_for(input_shape)
            y = K.reshape(y, (-1, max_length) + output_shape[2:])

        # Apply activity regularizer if any:
        if hasattr(self.layer, 'activity_regularizer') and self.layer.activity_regularizer is not None:
            regularization_loss = self.layer.activity_regularizer(y)
            self.add_loss(regularization_loss, X)
        return y


class Bidirectional(Wrapper):
    ''' Bidirectional wrapper for RNNs.

    # Arguments:
        layer: `Recurrent` instance.
        merge_mode: Mode by which outputs of the
            forward and backward RNNs will be combined.
            One of {'sum', 'mul', 'concat', 'ave', None}.
            If None, the outputs will not be combined,
            they will be returned as a list.

    # Examples:

    ```python
        model = Sequential()
        model.add(Bidirectional(LSTM(10, return_sequences=True), input_shape=(5, 10)))
        model.add(Bidirectional(LSTM(10)))
        model.add(Dense(5))
        model.add(Activation('softmax'))
        model.compile(loss='categorical_crossentropy', optimizer='rmsprop')
    ```
    '''
    def __init__(self, layer, merge_mode='concat', weights=None, **kwargs):
        if merge_mode not in ['sum', 'mul', 'ave', 'concat', None]:
            raise ValueError('Invalid merge mode. '
                             'Merge mode should be one of '
                             '{"sum", "mul", "ave", "concat", None}')
        self.forward_layer = layer
        config = layer.get_config()
        config['go_backwards'] = not config['go_backwards']
        self.backward_layer = layer.__class__.from_config(config)
        self.forward_layer.name = 'forward_' + self.forward_layer.name
        self.backward_layer.name = 'backward_' + self.backward_layer.name
        self.merge_mode = merge_mode
        if weights:
            nw = len(weights)
            self.forward_layer.initial_weights = weights[:nw // 2]
            self.backward_layer.initial_weights = weights[nw // 2:]
        self.stateful = layer.stateful
        self.return_sequences = layer.return_sequences
        self.supports_masking = True
        super(Bidirectional, self).__init__(layer, **kwargs)

    def get_weights(self):
        return self.forward_layer.get_weights() + self.backward_layer.get_weights()

    def set_weights(self, weights):
        nw = len(weights)
        self.forward_layer.set_weights(weights[:nw // 2])
        self.backward_layer.set_weights(weights[nw // 2:])

    def get_output_shape_for(self, input_shape):
        if self.merge_mode in ['sum', 'ave', 'mul']:
            return self.forward_layer.get_output_shape_for(input_shape)
        elif self.merge_mode == 'concat':
            shape = list(self.forward_layer.get_output_shape_for(input_shape))
            shape[-1] *= 2
            return tuple(shape)
        elif self.merge_mode is None:
            return [self.forward_layer.get_output_shape_for(input_shape)] * 2

    def call(self, X, mask=None):
        Y = self.forward_layer.call(X, mask)
        Y_rev = self.backward_layer.call(X, mask)
        if self.return_sequences:
            Y_rev = K.reverse(Y_rev, 1)
        if self.merge_mode == 'concat':
            return K.concatenate([Y, Y_rev])
        elif self.merge_mode == 'sum':
            return Y + Y_rev
        elif self.merge_mode == 'ave':
            return (Y + Y_rev) / 2
        elif self.merge_mode == 'mul':
            return Y * Y_rev
        elif self.merge_mode is None:
            return [Y, Y_rev]

    def reset_states(self):
        self.forward_layer.reset_states()
        self.backward_layer.reset_states()

    def build(self, input_shape):
        self.forward_layer.build(input_shape)
        self.backward_layer.build(input_shape)

    def compute_mask(self, input, mask):
        if self.return_sequences:
            if not self.merge_mode:
                return [mask, mask]
            else:
                return mask
        else:
            return None

    @property
    def trainable_weights(self):
        if hasattr(self.forward_layer, 'trainable_weights'):
            return self.forward_layer.trainable_weights + self.backward_layer.trainable_weights
        return []

    @property
    def non_trainable_weights(self):
        if hasattr(self.forward_layer, 'non_trainable_weights'):
            return self.forward_layer.non_trainable_weights + self.backward_layer.non_trainable_weights
        return []

    @property
    def updates(self):
        if hasattr(self.forward_layer, 'updates'):
            return self.forward_layer.updates + self.backward_layer.updates
        return []

    @property
    def losses(self):
        if hasattr(self.forward_layer, 'losses'):
            return self.forward_layer.losses + self.backward_layer.losses
        return []

    @property
    def constraints(self):
        _constraints = {}
        if hasattr(self.forward_layer, 'constraints'):
            _constraints.update(self.forward_layer.constraints)
            _constraints.update(self.backward_layer.constraints)
        return _constraints

    def get_config(self):
        config = {"merge_mode": self.merge_mode}
        base_config = super(Bidirectional, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))
