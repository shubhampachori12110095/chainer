import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy

import chainer
from chainer.backends import cuda
import chainer.functions.math.minmax
from chainer import initializers
import chainer.reporter
from chainer import testing
from chainer.testing import attr
import chainer.training.updaters.multiprocess_parallel_updater as mpu

import copy


class SimpleNet(chainer.Chain):
    insize = 5

    def __init__(self, dtype=numpy.float32):
        super(SimpleNet, self).__init__()
        self.dtype = dtype
        W = initializers.HeNormal(1 / numpy.sqrt(2), self.dtype)
        bias = initializers.Zero(self.dtype)
        with self.init_scope():
            self.conv = chainer.links.Convolution2D(2, 2, 3, initialW=W,
                                                    initial_bias=bias)
            self.fc = chainer.links.Linear(18, 2, initialW=W,
                                           initial_bias=bias)
        self.train = True

    def clear(self):
        self.loss = None
        self.accuracy = None

    def __call__(self, x, t):
        h = chainer.functions.relu(self.conv(x))
        y = self.fc(h)

        self.loss = chainer.functions.softmax_cross_entropy(y, t)
        self.accuracy = chainer.functions.accuracy(y, t)

        return self.loss


@testing.parameterize(*testing.product({
    'dtype': [numpy.float32, numpy.float16],
}))
class TestGatherScatter(unittest.TestCase):

    def setUp(self):
        pass

    @attr.gpu
    def test_gather_scatter_grads(self):
        cupy = cuda.cupy
        model0 = SimpleNet(dtype=self.dtype)
        model1 = copy.deepcopy(model0)

        model0.to_gpu()
        model1.to_gpu()

        optimizer0 = chainer.optimizers.SGD(lr=1.0)
        optimizer0.setup(model0)

        optimizer1 = chainer.optimizers.SGD(lr=1.0)
        optimizer1.setup(model1)

        bsize = 8

        x = numpy.random.uniform(0, 1, (bsize, 2, 5, 5)).astype(self.dtype)
        t = numpy.empty(bsize, dtype=numpy.int32)
        for i in range(bsize):
            t[i] = i % 2

        x = chainer.Variable(chainer.backends.cuda.to_gpu(x))
        t = chainer.Variable(chainer.backends.cuda.to_gpu(t))

        loss0 = model0(x, t)

        model0.cleargrads()
        model1.cleargrads()

        loss0.backward()
        gg0 = mpu.gather_grads(model0)
        mpu.scatter_grads(model1, gg0)

        cupy.testing.assert_array_equal(model0.conv.W.grad, model1.conv.W.grad)
        cupy.testing.assert_array_equal(model0.conv.b.grad, model1.conv.b.grad)
        cupy.testing.assert_array_equal(model0.fc.W.grad, model1.fc.W.grad)
        cupy.testing.assert_array_equal(model0.fc.b.grad, model1.fc.b.grad)

        optimizer0.update()
        optimizer1.update()

        cupy.testing.assert_array_equal(model0.conv.W.data, model1.conv.W.data)
        cupy.testing.assert_array_equal(model0.conv.b.data, model1.conv.b.data)
        cupy.testing.assert_array_equal(model0.fc.W.data, model1.fc.W.data)
        cupy.testing.assert_array_equal(model0.fc.b.data, model1.fc.b.data)

    def test_gather_grads_raise_on_cpu(self):
        model = SimpleNet(dtype=self.dtype)
        with self.assertRaises(RuntimeError):
            mpu.gather_grads(model)

    @attr.gpu
    def test_gather_scatter_params(self):
        cupy = cuda.cupy
        model0 = SimpleNet(dtype=self.dtype)
        model1 = SimpleNet(dtype=self.dtype)

        model0.to_gpu()
        model1.to_gpu()

        gp0 = mpu.gather_params(model0)
        mpu.scatter_params(model1, gp0)

        cupy.testing.assert_array_equal(model0.conv.W.data, model1.conv.W.data)
        cupy.testing.assert_array_equal(model0.conv.b.data, model1.conv.b.data)
        cupy.testing.assert_array_equal(model0.fc.W.data, model1.fc.W.data)
        cupy.testing.assert_array_equal(model0.fc.b.data, model1.fc.b.data)

    def test_gather_params_raise_on_cpu(self):
        model = SimpleNet(dtype=self.dtype)
        with self.assertRaises(RuntimeError):
            mpu.gather_params(model)


class SimpleNetRawArray(chainer.Chain):

    def __init__(self, testcase):
        super(SimpleNetRawArray, self).__init__()
        with self.init_scope():
            self.conv = chainer.links.Convolution2D(2, 2, 3)
            self.fc = chainer.links.Linear(18, 2)

        self.train = True
        self.call_called = 0
        self.testcase = testcase

    def clear(self):
        self.loss = None
        self.accuracy = None

    def __call__(self, x, t):
        self.testcase.assertNotIsInstance(x, chainer.Variable)
        self.testcase.assertNotIsInstance(t, chainer.Variable)

        self.call_called += 1

        h = chainer.functions.relu(self.conv(x))
        y = self.fc(h)

        self.loss = chainer.functions.softmax_cross_entropy(y, t)
        self.accuracy = chainer.functions.accuracy(y, t)

        return self.loss


class TestRawArray(unittest.TestCase):

    def setUp(self):
        pass

    @attr.gpu
    def test_update_uses_raw_array(self):
        if mpu.MultiprocessParallelUpdater.available():
            model = SimpleNetRawArray(self)
            dataset = [((numpy.ones((2, 5, 5)) * i).astype(numpy.float32),
                        numpy.int32(0)) for i in range(100)]

            batch_size = 5
            devices = (0,)
            iters = [chainer.iterators.SerialIterator(i, batch_size) for i in
                     chainer.datasets.split_dataset_n_random(
                         dataset, len(devices))]
            optimizer = chainer.optimizers.SGD(lr=1.0)
            optimizer.setup(model)

            with testing.assert_warns(UserWarning):
                updater = mpu.MultiprocessParallelUpdater(
                    iters, optimizer, devices=devices)
            updater.update()

            self.assertEqual(model.call_called, 1)


class TestChildReporter(unittest.TestCase):

    def check_update_uses_raw_array(self, n_devices):
        device_ids_tuple = 'tuple([{}])'.format(
            ', '.join([str(n) for n in range(n_devices)]))
        code = '''
import numpy
import chainer
from chainer.training import trainer
import chainer.training.updaters.multiprocess_parallel_updater as mpu
class SimpleNetChild(chainer.Chain):

    def __init__(self):
        super(SimpleNetChild, self).__init__()
        with self.init_scope():
            self.conv = chainer.links.Convolution2D(2, 2, 3)

    def __call__(self, x):

        h = chainer.functions.relu(self.conv(x))

        chainer.reporter.report({
            'h_max': chainer.functions.math.minmax.max(h)}, self)

        return h


class SimpleNetChildReporter(chainer.Chain):

    def __init__(self):
        super(SimpleNetChildReporter, self).__init__()
        with self.init_scope():
            self.c1 = SimpleNetChild()
            self.fc = chainer.links.Linear(18, 2)
        self.call_called = 0

    def clear(self):
        self.loss = None

    def __call__(self, x, t):

        self.call_called += 1

        h = chainer.functions.relu(self.c1(x))
        y = self.fc(h)

        self.loss = chainer.functions.softmax_cross_entropy(y, t)
        chainer.reporter.report({'loss': self.loss}, self)

        return self.loss

if __name__ == '__main__':
    model = SimpleNetChildReporter()
    dataset = [(numpy.full((2, 5, 5), i, numpy.float32),
                numpy.int32(0)) for i in range(100)]

    batch_size = 5
    devices = {{{device_ids_tuple}}}
    iters = [chainer.iterators.SerialIterator(i, batch_size) for i in
             chainer.datasets.split_dataset_n_random(
                 dataset, len(devices))]
    optimizer = chainer.optimizers.SGD(lr=1.0)
    optimizer.setup(model)
    updater = mpu.MultiprocessParallelUpdater(
        iters, optimizer, devices=devices)
    trainer = trainer.Trainer(updater, (1, 'iteration'), '/tmp')
    trainer.run()
    assert model.call_called == 1
'''.replace('{{{device_ids_tuple}}}', device_ids_tuple)

        temp_dir = tempfile.mkdtemp()
        try:
            script_path = os.path.join(temp_dir, 'script.py')
            with open(script_path, 'w') as f:
                f.write(code)
            proc = subprocess.Popen(
                [sys.executable, script_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            stdoutdata, stderrdata = proc.communicate()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        ret = proc.returncode
        assert ret == 0, (
            'Import test failed.\n'
            '[code]:\n{}\n'
            '[stdout]:{!r}\n'
            '[stderr]:{!r}'.format(
                code, stdoutdata, stderrdata))

    @attr.gpu
    @unittest.skipUnless(mpu.MultiprocessParallelUpdater.available(),
                         'MultiprocessParallelUpdater is not available.')
    def test_single_device(self):
        self.check_update_uses_raw_array(1)

    @attr.multi_gpu(2)
    @unittest.skipUnless(mpu.MultiprocessParallelUpdater.available(),
                         'MultiprocessParallelUpdater is not available.')
    def test_multi_device(self):
        self.check_update_uses_raw_array(2)


testing.run_module(__name__, __file__)
