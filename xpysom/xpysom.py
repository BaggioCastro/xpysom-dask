from math import sqrt, ceil
from collections import defaultdict
from warnings import warn
from collections import defaultdict, Counter
from warnings import warn
from sys import stdout
from time import time
from datetime import timedelta
import pickle
import os

import numpy as np
try:
    # Cupy needs to be imported first.
    # Cudf is crashing containers if it goes first.
    import cupy as cp
    import cudf
    import dask_cudf as dcudf
    default_xp = cp
    GPU_SUPPORTED=True
except:
    print("WARNING: CuPy could not be imported")
    default_xp = np
    GPU_SUPPORTED=False

try:
    import dask
    import dask.array as da
    import dask.delayed as dd
    import dask.dataframe as ddf
    default_da = True
except:
    print("WARNING: Dask Arrays could not be imported")
    default_da = False

from .distances import cosine_distance, manhattan_distance, euclidean_squared_distance, euclidean_squared_distance_part, euclidean_distance
from .neighborhoods import gaussian_generic, gaussian_rect, mexican_hat_generic, mexican_hat_rect, bubble, triangle, prepare_neig_func
from .utils import find_cpu_cores, find_max_cuda_threads
from .decays import linear_decay, asymptotic_decay, exponential_decay

# In my machine it looks like these are the best performance/memory trade-off.
# As a rule of thumb, executing more items at a time does not decrease 
# performance but it may increase the memory footprint without providing 
# significant gains.
DEFAULT_CPU_CORE_OVERSUBSCRIPTION = 500

beginning = None
sec_left = None

def print_progress(t, T):
    digits = len(str(T))

    global beginning, sec_left

    if t == -1:
        progress = '\r [ {s:{d}} / {T} ] {s:3.0f}% - ? it/s'
        progress = progress.format(T=T, d=digits, s=0)
        stdout.write(progress)
        beginning = time()
    else:
        sec_left = ((T-t+1) * (time() - beginning)) / (t+1)
        time_left = str(timedelta(seconds=sec_left))[:7]
        sec_elapsed = time() - beginning
        time_elapsed = str(timedelta(seconds=sec_elapsed))[:7]
        progress = '\r [ {t:{d}} / {T} ]'.format(t=t+1, d=digits, T=T)
        progress += ' {p:3.0f}%'.format(p=100*(t+1)/T)
        progress += ' - {time_elapsed} elapsed '.format(time_elapsed=time_elapsed)
        progress += ' - {time_left} left '.format(time_left=time_left)
        stdout.write(progress)


class XPySom:
    def __init__(self, x, y, input_len, 
                 sigma=0, sigmaN=1, 
                 learning_rate=0.5, learning_rateN=0.01, decay_function='exponential',
                 neighborhood_function='gaussian', std_coeff=0.5, 
                 topology='rectangular', 
                 activation_distance='euclidean', 
                 random_seed=None, n_parallel=0, compact_support=False,
                 xp=default_xp,
                 use_dask=False, dask_chunks='auto'):
        """Initializes a Self Organizing Maps.

        A rule of thumb to set the size of the grid for a dimensionality
        reduction task is that it should contain 5*sqrt(N) neurons
        where N is the number of samples in the dataset to analyze.

        E.g. if your dataset has 150 samples, 5*sqrt(150) = 61.23
        hence a map 8-by-8 should perform well.

        Parameters
        ----------
        x : int
            x dimension of the SOM.

        y : int
            y dimension of the SOM.

        input_len : int
            Number of the elements of the vectors in input.

        sigma : float, optional (default=min(x,y)/2)
            Spread of the neighborhood function, needs to be adequate
            to the dimensions of the map.

        sigmaN : float, optional (default=0.01)
            Spread of the neighborhood function at last iteration.

        learning_rate : float, optional (default=0.5)
            initial learning rate.

        learning_rateN : float, optional (default=0.01)
            final learning rate

        decay_function : string, optional (default='exponential')
            Function that reduces learning_rate and sigma at each iteration.
            Possible values: 'exponential', 'linear', 'aymptotic'

        neighborhood_function : string, optional (default='gaussian')
            Function that weights the neighborhood of a position in the map.
            Possible values: 'gaussian', 'mexican_hat', 'bubble', 'triangle'

        topology : string, optional (default='rectangular')
            Topology of the map.
            Possible values: 'rectangular', 'hexagonal'

        activation_distance : string, optional (default='euclidean')
            Distance used to activate the map.
            Possible values: 'euclidean', 'cosine', 'manhattan'

        random_seed : int, optional (default=None)
            Random seed to use.

        n_parallel : uint, optionam (default=#max_CUDA_threads or 500*#CPUcores)
            Number of samples to be processed at a time. Setting a too low 
            value may drastically lower performance due to under-utilization,
            setting a too high value increases memory usage without granting 
            any significant performance benefit.

        xp : numpy or cupy, optional (default=cupy if can be imported else numpy)
            Use numpy (CPU) or cupy (GPU) for computations.
        
        std_coeff: float, optional (default=0.5)
            Used to calculate gausssian exponent denominator: 
            d = 2*std_coeff**2*sigma**2

        compact_support: bool, optional (default=False)
            Cut the neighbor function to 0 beyond neighbor radius sigma

        use_dask: bool, optional (default=False)
            Use a distributed SOM based on Dask clustering

        dask_chunks: tuple, optional (default='auto')
            The size of the data chunks that it will be splited up

        """

        if sigma >= x or sigma >= y:
            warn('Warning: sigma is too high for the dimension of the map.')

        self._random_generator = np.random.RandomState(random_seed)

        self.xp = xp

        # Use dask for clustering SOM
        self.use_dask = use_dask & default_da
        self.dask_chunks = dask_chunks

        self._learning_rate = learning_rate
        self._learning_rateN = learning_rateN

        if sigma == 0:
            self._sigma = min(x,y)/2
        else:
            self._sigma = sigma

        self._std_coeff = std_coeff

        self._sigmaN = sigmaN
        self._input_len = input_len

        # random initialization
        self._weights = self._random_generator.rand(x, y, input_len)*2-1
        self._weights /= np.linalg.norm(self._weights, axis=-1, keepdims=True)

        # used to evaluate the neighborhood function
        self._neigx = self.xp.arange(x)
        self._neigy = self.xp.arange(y)  

        if topology not in ['hexagonal', 'rectangular']:
            msg = '%s not supported only hexagonal and rectangular available'
            raise ValueError(msg % topology)

        self.topology = topology
        self._xx, self._yy = self.xp.meshgrid(self._neigx, self._neigy)
        self._xx = self._xx.astype(float)
        self._yy = self._yy.astype(float)

        if topology == 'hexagonal':
            self._xx[::-2] -= 0.5
            if neighborhood_function in ['triangle']:
                warn('triangle neighborhood function does not ' +
                     'take in account hexagonal topology')

        decay_functions = {
            'exponential': exponential_decay,
            'asymptotic': asymptotic_decay,
            'linear': linear_decay
        }

        if decay_function not in decay_functions:
            msg = '%s not supported. Functions available: %s'
            raise ValueError(msg % (decay_function,
                                    ', '.join(decay_functions.keys())))

        self._decay_function = decay_functions[decay_function]

        self.compact_support = compact_support

        neig_functions = self.get_neig_functions()

        if neighborhood_function not in neig_functions:
            msg = '%s not supported. Functions available: %s'
            raise ValueError(msg % (neighborhood_function,
                                    ', '.join(neig_functions.keys())))

        self.neighborhood = neig_functions[neighborhood_function]
        self.neighborhood_func_name = neighborhood_function

        distance_functions = {
            'euclidean': euclidean_squared_distance_part,
            'euclidean_no_opt': euclidean_squared_distance,
            'manhattan': manhattan_distance,
            'cosine': cosine_distance,
        }

        if activation_distance not in distance_functions:
            msg = '%s not supported. Distances available: %s'
            raise ValueError(msg % (activation_distance,
                                    ', '.join(distance_functions.keys())))

        self._activation_distance = distance_functions[activation_distance]

        self._unravel_precomputed = self.xp.unravel_index(self.xp.arange(x*y, dtype=self.xp.int64), (x,y))

        if n_parallel == 0:
            if self.xp.__name__ == 'cupy':
                n_parallel = find_max_cuda_threads()
            else:
                n_parallel = find_cpu_cores()*DEFAULT_CPU_CORE_OVERSUBSCRIPTION  
 
            if n_parallel == 0:
                raise ValueError("n_parallel was not specified and could not be infered from system")

        self._n_parallel = n_parallel

        self._sq_weights_gpu = None

    
    def get_neig_functions(self):
        """
        Returns a dictionary (func_name, prepared_func)
        Call this only after setting neigx, neigy, xx, yy.
        """
        if self.topology == 'rectangular':
            neig_functions = {
                'gaussian': prepare_neig_func(
                    gaussian_rect, self._neigx, self._neigy, self._std_coeff, self.compact_support),
                'mexican_hat': prepare_neig_func(
                    mexican_hat_rect, self._neigx, self._neigy, self._std_coeff, self.compact_support),
                'bubble': prepare_neig_func(
                    bubble, self._neigx, self._neigy),
                'triangle': prepare_neig_func(
                    triangle, self._neigx, self._neigy, self.compact_support),
            }
        elif self.topology == 'hexagonal':
            neig_functions = {
                'gaussian': prepare_neig_func(
                    gaussian_generic, self._xx, self._yy, self._std_coeff, self.compact_support),
                'mexican_hat': prepare_neig_func(
                    mexican_hat_generic, self._xx, self._yy, self._std_coeff, self.compact_support),
                'bubble': prepare_neig_func(
                    bubble, self._neigx, self._neigy),
            }
        else:
            neig_functions = {}

        return neig_functions


    def get_weights(self):
        """Returns the weights of the neural network."""
        return self._weights


    def get_euclidean_coordinates(self):
        """Returns the position of the neurons on an euclidean
        plane that reflects the chosen topology in two meshgrids xx and yy.
        Neuron with map coordinates (1, 4) has coordinate (xx[1, 4], yy[1, 4])
        in the euclidean plane.

        Only useful if the topology chosen is not rectangular.
        """
        if GPU_SUPPORTED:
            if isinstance(self._xx.T, cp.ndarray) and \
               isinstance(self._yy.T, cp.ndarray):
                # I need to transfer them to host
                return self._xx.T.get(), self._yy.T.get()

        return self._xx.T, self._yy.T


    def convert_map_to_euclidean(self, xy):
        """Converts map coordinates into euclidean coordinates
        that reflects the chosen topology.

        Only useful if the topology chosen is not rectangular.
        """
        if GPU_SUPPORTED:
            if isinstance(self._xx.T, cp.ndarray) and \
               isinstance(self._yy.T, cp.ndarray):
                # I need to transfer them to host
                return self._xx.T.get()[xy], self._yy.T.get()[xy]

        return self._xx.T[xy], self._yy.T[xy]


    def activate(self, x):
        """Returns the activation map to x."""
        x_gpu = self.xp.array(x)
        weights_gpu = self.xp.array(self._weights)

        self._activate(x_gpu, weights_gpu)

        if GPU_SUPPORTED and isinstance(self._activation_map_gpu, cp.ndarray):
            return self._activation_map_gpu.get()
        else:
            return self._activation_map_gpu


    def _activate(self, x_gpu, weights_gpu):
        """Updates matrix activation_map, in this matrix
           the element i,j is the response of the neuron i,j to x"""
        if len(x_gpu.shape) == 1:
            x_gpu = self.xp.expand_dims(x_gpu, axis=0)

        if self._sq_weights_gpu is not None:
            self._activation_map_gpu = self._activation_distance(
                    x_gpu, 
                    weights_gpu,
                    self._sq_weights_gpu,
                    xp=self.xp
            )
        else:
            self._activation_map_gpu = self._activation_distance(
                    x_gpu, 
                    weights_gpu,
                    xp=self.xp
            )


    def _check_iteration_number(self, num_iteration):
        if num_iteration < 1:
            raise ValueError('num_iteration must be > 1')


    def _check_input_len(self, data):
        """Checks that the data in input is of the correct shape."""
        data_len = len(data[0])
        if self._input_len != data_len:
            msg = 'Received %d features, expected %d.' % (data_len,
                                                          self._input_len)
            raise ValueError(msg)


    def winner(self, x):
        """Computes the coordinates of the winning neurons for the samples x.
        """

        if self.use_dask:
            x_gpu = da.from_array(self.xp.array(x))
        else:
            x_gpu = self.xp.array(x)

        weights_gpu = self.xp.array(self._weights)

        orig_shape = x_gpu.shape
        if len(orig_shape) == 1:
            if isinstance(x_gpu, da.core.Array):
                x_gpu = da.expand_dims(x_gpu, axis=0).compute()
            else:
                x_gpu = self.xp.expand_dims(x_gpu, axis=0)

        winners_chunks = []
        for i in range(0, len(x), self._n_parallel):
            start = i
            end = start + self._n_parallel
            if end > len(x):
                end = len(x)

            chunk = self._winner(x_gpu[start:end], weights_gpu)
            winners_chunks.append(self.xp.vstack(chunk))

        winners_gpu = self.xp.hstack(winners_chunks)

        if GPU_SUPPORTED and isinstance(winners_gpu, cp.ndarray):
            winners = winners_gpu.get()
        else:
            winners = winners_gpu

        if len(orig_shape) == 1:
            return (winners[0].item(), winners[1].item())
        else:
            return list(map(tuple, winners.T))

    def _winner(self, x_gpu, winners_gpu):
        """Computes the coordinates of the winning neuron for the sample x"""
        if len(x_gpu.shape) == 1:
            x_gpu = self.xp.expand_dims(x_gpu, axis=0)

        self._activate(x_gpu, winners_gpu)
        raveled_idxs = self._activation_map_gpu.argmin(axis=1)
        return (self._unravel_precomputed[0][raveled_idxs], self._unravel_precomputed[1][raveled_idxs])


    def _update(self, x_gpu, weights_gpu, eta, sig):
        """Updates the numerator and denominator accumulators.

        Parameters
        ----------
        x : np.array
            Current pattern to learn
        t : int
            Iteration index
        """
        weights_gpu = self.xp.asarray(weights_gpu)

        wins = self._winner(x_gpu, weights_gpu)

        g_gpu = self.neighborhood(wins, sig, xp=self.xp)*eta

        sum_g_gpu = self.xp.sum(g_gpu, axis=0)
        g_flat_gpu = g_gpu.reshape(g_gpu.shape[0], -1)
        gT_dot_x_flat_gpu = self.xp.dot(g_flat_gpu.T, x_gpu)

        _numerator_gpu = gT_dot_x_flat_gpu.reshape(weights_gpu.shape)
        _denominator_gpu = sum_g_gpu[:,:,self.xp.newaxis]

        return (_numerator_gpu, _denominator_gpu)


    def _merge_updates(self, weights_gpu, numerator_gpu, denominator_gpu):
        """
        Divides the numerator accumulator by the denominator accumulator 
        to compute the new weights. 
        """
        return self.xp.where(
            denominator_gpu != 0,
            numerator_gpu / denominator_gpu,
            weights_gpu
        )


    def train(self, data, num_epochs, iter_beg=0, iter_end=None, verbose=False):
        """Trains the SOM.

        Parameters
        ----------
        data : np.array or list
            Data matrix.

        num_epochs : int
            Maximum number of epochs (one epoch = all samples).
            In the code iteration and epoch have the same meaning.

        iter_beg : int, optional (default=0)
            Start from iteration at index iter_beg

        iter_end : int, optional (default=None, i.e. num_epochs)
            End before iteration iter_end (excluded) or after num_epochs
            if iter_end is None.

        verbose : bool (default=False)
            If True the status of the training
            will be printed at each iteration.
        """
        if iter_end is None:
            iter_end = num_epochs

        # Copy arrays to device
        weights_gpu = self.xp.asarray(self._weights, dtype=self.xp.float32)

        if GPU_SUPPORTED and isinstance(data, cudf.core.dataframe.DataFrame):
            data_gpu = data.to_cupy(dtype=self.xp.float32)
            if self.use_dask:
                data_gpu_block = da.from_array(data_gpu, chunks=self.dask_chunks)
        elif GPU_SUPPORTED and isinstance(data, cp._core.core.ndarray):
            data_gpu = data.astype(self.xp.float32)
            if self.use_dask:
                data_gpu_block = da.from_array(data_gpu, chunks=self.dask_chunks)
        elif default_da and isinstance(data, ddf.core.DataFrame):
            if self.use_dask:
                data_gpu_block = data.to_dask_array()
            else:
                data_gpu = data.to_dask_array().compute()
        elif GPU_SUPPORTED and isinstance(data, dcudf.core.DataFrame):
            if self.use_dask:
                data_gpu = data.to_dask_array()
            data_gpu = data.compute()
        elif default_da and isinstance(data, da.core.Array):
            if self.use_dask:
                data_gpu_block = data
            else:
                data_gpu = data.compute().astype(self.xp.float32)
        else:
            data_gpu = self.xp.asarray(data, dtype=self.xp.float32)

        if verbose:
            print_progress(-1, num_epochs*len(data))

        for iteration in range(iter_beg, iter_end):
            try: # reuse already allocated memory
                numerator_gpu.fill(0)
                denominator_gpu.fill(0)
            except UnboundLocalError: # whoops, I haven't allocated it yet
                numerator_gpu = self.xp.zeros(
                    weights_gpu.shape,
                    dtype=self.xp.float32
                )
                denominator_gpu = self.xp.zeros(
                    (weights_gpu.shape[0], weights_gpu.shape[1],1),
                    dtype=self.xp.float32
                )

            if self._activation_distance in [
                    euclidean_squared_distance,
                    euclidean_squared_distance_part,
                    cosine_distance
            ]:
                self._sq_weights_gpu = (
                    self.xp.power(
                        weights_gpu.reshape(
                            -1, weights_gpu.shape[2]
                        ),
                        2
                    ).sum(axis=1, keepdims=True)
                )
            else:
                self._sq_weights_gpu = None

            eta = self._decay_function(self._learning_rate, self._learning_rateN, iteration, num_epochs)
            # sigma and learning rate decrease with the same rule
            sig = self._decay_function(self._sigma, self._sigmaN, iteration, num_epochs)

            if self.use_dask:
                blocks = data_gpu_block.to_delayed().ravel()

                numerator_gpu_array = []
                denominator_gpu_array = []
                for block in blocks:
                    a, b = dask.delayed(self._update, nout=2)(block, weights_gpu, eta, sig)
                    numerator_gpu_array.append(a)
                    denominator_gpu_array.append(b)

                numerator_gpu_sum = dask.delayed(sum)(numerator_gpu_array)
                denominator_gpu_sum = dask.delayed(sum)(denominator_gpu_array)

                numerator_gpu, denominator_gpu = dask.compute(numerator_gpu_sum, denominator_gpu_sum)
            else:
                for i in range(0, len(data), self._n_parallel):
                    start = i
                    end = start + self._n_parallel
                    if end > len(data):
                        end = len(data)

                    a, b = self._update(data_gpu[start:end], weights_gpu, eta, sig)

                    numerator_gpu += a
                    denominator_gpu += b

                    if verbose:
                        print_progress(
                            iteration*len(data)+end-1,
                            num_epochs*len(data)
                        )

            weights_gpu = self._merge_updates(weights_gpu, numerator_gpu, denominator_gpu)

        # Copy back arrays to host
        if GPU_SUPPORTED and isinstance(weights_gpu, cp.ndarray):
            self._weights = weights_gpu.get()
        else:
            self._weights = weights_gpu

        # free temporary memory
        self._sq_weights_gpu = None

        if hasattr(self, '_activation_map_gpu'):
            del self._activation_map_gpu

        if verbose:
            print('\n quantization error:', self.quantization_error(data))

        return self


    def train_batch(self, data, num_iteration, verbose=False):
        """Compatibility with MiniSom, alias for train"""
        return self.train(data, num_iteration, verbose=verbose)


    def train_random(self, data, num_iteration, verbose=False):
        """Compatibility with MiniSom, alias for train"""
        print("WARNING: due to batch SOM algorithm, random order is not supported. Falling back to train_batch.")
        return self.train(data, num_iteration, verbose=verbose)


    def predict(self, data):
        def _predict(data, xp):
            shape = (self._weights.shape[0], self._weights.shape[1])
            winner_coordinates = xp.array([self.winner(x) for x in data]).T
            return xp.asarray(xp.ravel_multi_index(winner_coordinates, shape))

        if default_da and isinstance(data, da.core.Array):
            if self.use_dask:
                return data.map_blocks(_predict, self.xp, dtype=self.xp.float32, meta=self.xp.array((), dtype=self.xp.float32))
        return _predict(data, self.xp)


    def quantization(self, data):
        """Assigns a code book (weights vector of the winning neuron)
        to each sample in data."""
        
        data_gpu = self.xp.array(data)
        qnt = self._quantization(data_gpu, self.xp.array(self._weights))

        if GPU_SUPPORTED and isinstance(qnt, cp.ndarray):
            return qnt.get()
        else:
            return qnt


    def _quantization(self, data_gpu, weights_gpu):
        """Assigns a code book (weights vector of the winning neuron)
        to each sample in data."""
        self._check_input_len(data_gpu)
        winners_coords = self.xp.argmin(self._distance_from_weights(data_gpu, weights_gpu), axis=1)
        return weights_gpu[self.xp.unravel_index(winners_coords,
                           self._weights.shape[:2])].copy()

    def distance_from_weights(self, data, weights_gpu):
        """Returns a matrix d where d[i,j] is the euclidean distance between
        data[i] and the j-th weight.
        """
        data_gpu = self.xp.array(data)
        weights_gpu = self.xp.array(self._weights)
        d = self._distance_from_weights(data_gpu, weights_gpu)

        if GPU_SUPPORTED and isinstance(d, cp.ndarray):
            return d.get()
        else:
            return d

    def _distance_from_weights(self, data_gpu, weights):
        """Returns a matrix d where d[i,j] is the euclidean distance between
        data[i] and the j-th weight.
        """
        distances = []
        for start in range(0, len(data_gpu), self._n_parallel):
            end = start + self._n_parallel
            if end > len(data_gpu):
                end = len(data_gpu)

            distances.append(euclidean_distance(data_gpu[start:end], weights, xp=self.xp))
        return self.xp.vstack(distances)

    def quantization_error(self, data):
        """Returns the quantization error computed as the average
        distance between each input sample and its best matching unit."""
        self._check_input_len(data)

        if self.use_dask:
            if default_da and isinstance(data, da.core.Array):
                data_gpu = data
            else:
                data_gpu = da.from_array(self.xp.array(data, dtype=self.xp.float32), chunks=self.dask_chunks)

            blocks = data_gpu

            def _quantization_error_block(block, weights):
                weights_gpu = self.xp.array(weights)

                new_block = block - self._quantization(block, weights_gpu)

                return new_block

            q_error = blocks.map_blocks(_quantization_error_block, self._weights, dtype=self.xp.float32)

            qe_lin = da.linalg.norm(q_error, axis=1)
            qe = qe_lin.mean().compute()
        else:
            # load to GPU
            data_gpu = self.xp.array(data, dtype=self.xp.float32)
            weights_gpu = self.xp.array(self._weights)

            # recycle buffer
            data_gpu -= self._quantization(data_gpu, weights_gpu)

            qe = self.xp.linalg.norm(data_gpu, axis=1).mean()

        return qe.item()

    def topographic_error(self, data):
        """Returns the topographic error computed by finding
        the best-matching and second-best-matching neuron in the map
        for each input and then evaluating the positions.

        A sample for which these two nodes are not ajacent conunts as
        an error. The topographic error is given by the
        the total number of errors divided by the total of samples.

        If the topographic error is 0, no error occurred.
        If 1, the topology was not preserved for any of the samples."""
        self._check_input_len(data)
        total_neurons = np.prod(self._weights.shape)
        if total_neurons == 1:
            warn('The topographic error is not defined for a 1-by-1 map.')
            return np.nan

        # load to GPU
        data_gpu = self.xp.array(data, dtype=self.xp.float32)

        weights_gpu = self.xp.array(self._weights)

        distances = self._distance_from_weights(data_gpu, weights_gpu)

        # b2mu: best 2 matching units
        b2mu_inds = self.xp.argsort(distances, axis=1)[:, :2]
        b2my_xy = self.xp.unravel_index(b2mu_inds, self._weights.shape[:2])
        if self.topology ==  'rectangular':
            b2mu_x, b2mu_y = b2my_xy[0], b2my_xy[1]
            diff_b2mu_x = self.xp.abs(self.xp.diff(b2mu_x))
            diff_b2mu_y = self.xp.abs(self.xp.diff(b2mu_y))
            return ((diff_b2mu_x > 1) | (diff_b2mu_y > 1)).mean().item()
        elif self.topology == 'hexagonal':
            b2mu_x = self._xx[b2my_xy[0], b2my_xy[1]]
            b2mu_y = self._yy[b2my_xy[0], b2my_xy[1]]
            dxdy = self.xp.hstack([self.xp.diff(b2mu_x), self.xp.diff(b2mu_y)])
            distance = self.xp.linalg.norm(dxdy, axis=1)
            return (distance > 1.5).mean().item()


    def random_weights_init(self, data):
        """Initializes the weights of the SOM
        picking random samples from data.
        TODO: unoptimized
        """
        self._check_input_len(data)
        it = np.nditer(self._weights[:,:,0], flags=['multi_index'])
        while not it.finished:
            rand_i = self._random_generator.randint(len(data))
            self._weights[it.multi_index] = data[rand_i]
            it.iternext()


    def pca_weights_init(self, data):
        """Initializes the weights to span the first two principal components.

        This initialization doesn't depend on random processes and
        makes the training process converge faster.

        It is strongly reccomended to normalize the data before initializing
        the weights and use the same normalization for the training data.

        TODO: unoptimized
        """
        if self._input_len == 1:
            msg = 'The data needs at least 2 features for pca initialization'
            raise ValueError(msg)
        self._check_input_len(data)
        if len(self._neigx) == 1 or len(self._neigy) == 1:
            msg = 'PCA initialization inappropriate:' + \
                  'One of the dimensions of the map is 1.'
            warn(msg)
        pc_length, pc = np.linalg.eig(np.cov(np.transpose(data)))
        pc_order = np.argsort(-pc_length)
        for i, c1 in enumerate(np.linspace(-1, 1, len(self._neigx))):
            for j, c2 in enumerate(np.linspace(-1, 1, len(self._neigy))):
                self._weights[i, j] = c1*pc[pc_order[0]] + c2*pc[pc_order[1]]


    def distance_map(self):
        """Returns the distance map of the weights.
        Each cell is the normalised sum of the distances between
        a neuron and its neighbours. Note that this method uses
        the euclidean distance.
        TODO: unoptimized
        """
        um = np.zeros((self._weights.shape[0],
                    self._weights.shape[1],
                    8))  # 2 spots more for hexagonal topology

        ii = [[0, -1, -1, -1, 0, 1, 1, 1]]*2
        jj = [[-1, -1, 0, 1, 1, 1, 0, -1]]*2

        if self.topology == 'hexagonal':
            ii = [[1, 1, 1, 0, -1, 0], [0, 1, 0, -1, -1, -1]]
            jj = [[1, 0, -1, -1, 0, 1], [1, 0, -1, -1, 0, 1]]

        for x in range(self._weights.shape[0]):
            for y in range(self._weights.shape[1]):
                w_2 = self._weights[x, y]
                e = y % 2 == 0   # only used on hexagonal topology
                for k, (i, j) in enumerate(zip(ii[e], jj[e])):
                    if (x+i >= 0 and x+i < self._weights.shape[0] and
                            y+j >= 0 and y+j < self._weights.shape[1]):
                        w_1 = self._weights[x+i, y+j]
                        um[x, y, k] = np.linalg.norm(w_2-w_1)

        um = um.sum(axis=2)
        return um/um.max()

    def activation_response(self, data):
        """
        Returns a matrix where the element i,j is the number of times
        that the neuron i,j have been winner.
        """
        self._check_input_len(data)
        a = np.zeros((self._weights.shape[0], self._weights.shape[1]))
        winners = self.winner(data)
        for win in winners:
            a[win] += 1
        return a

    def win_map(self, data):
        """Returns a dictionary wm where wm[(i,j)] is a list
        with all the patterns that have been mapped in the position i,j.
        """
        self._check_input_len(data)
        winmap = defaultdict(list)
        winners = self.winner(data)
        for x, win in zip(data, winners):
            winmap[win].append(x)
        return winmap

    def labels_map(self, data, labels):
        """Returns a dictionary wm where wm[(i,j)] is a dictionary
        that contains the number of samples from a given label
        that have been mapped in position i,j.

        Parameters
        ----------
        data : np.array or list
            Data matrix.

        label : np.array or list
            Labels for each sample in data.

        """
        self._check_input_len(data)
        if not len(data) == len(labels):
            raise ValueError('data and labels must have the same length.')
        winmap = defaultdict(list)
        winners = self.winner(data)
        for win, l in zip(winners, labels):
            winmap[win].append(l)
        for position in winmap:
            winmap[position] = Counter(winmap[position])
        return winmap


    def __getstate__(self):
        # Copy the object's state from self.__dict__ which contains
        # all our instance attributes. Always use the dict.copy()
        # method to avoid modifying the original state.
        state = self.__dict__.copy()
        # Remove the unpicklable entries.
        del state['xp']
        del state['neighborhood']
        state['xp_name'] = self.xp.__name__
        return state

    def __setstate__(self, state):
        # Restore instance attributes (i.e., filename and lineno).
        self.__dict__.update(state)
        try:
            if self.xp_name == 'cupy':
                self.xp = cp
            elif self.xp_name == 'numpy':
                self.xp = np
        except:
            self.xp = default_xp

        self.neighborhood = self.get_neig_functions()[self.neighborhood_func_name]
