# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Coder classes for encoding/decoding TF Examples into tf.Transform datasets.
"""

# TODO(b/33688275): Rename ExampleProto to just Example, for all aspects of this
# API (eg Classes, Files, Benchmarks etc).


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# GOOGLE-INITIALIZATION

import numpy as np
import six
import tensorflow as tf
from tensorflow_transform.tf_metadata import schema_utils


# This function needs to be called at pipeline execution time as it depends on
# the protocol buffer library installed in the workers (which might be different
# from the one installed in the pipeline constructor).
#
# TODO(b/35573758): Simplify this once the 'python' implementation of the
# protocol buffer API can handle the numpy type conversions properly.
def _make_cast_fn(np_dtype):
  """Return a function to extract the typed value from the feature.

  For performance reasons it is preferred to have the cast fn
  constructed once (for each handler).

  Args:
    np_dtype: The numpy type of the Tensorflow feature.
  Returns:
    A function to extract the value field from a string depending on dtype.
  """
  # There seems to be a great degree of variability for handling automatic
  # conversions across types and across API implementation of the Python
  # protocol buffer library.
  #
  # For the 'python' implementation we need to always "cast" from np types to
  # the appropriate Python type.
  #
  # For the 'cpp' implementation we need to only "cast" from np types to the
  # appropriate Python type for "Float" types, but only for protobuf < 3.2.0

  def identity(x):
    return x

  def numeric_cast(x):
    if isinstance(x, (np.generic, np.ndarray)):
      # This works for both np.generic and np.array (of any shape).
      return x.tolist()
    else:
      # This works for python scalars (or lists thereof), which require no
      # casting.
      return x

  # This is in agreement with Tensorflow conversions for Unicode values for both
  # Python 2 and 3 (and also works for non-Unicode objects). It is also in
  # agreement with the testWithUnicode of the Beam impl.
  def utf8(s):
    return s if isinstance(s, bytes) else s.encode('utf-8')

  def string_cast(x):
    return map(utf8, x) if isinstance(x, (list, np.ndarray)) else utf8(x)

  if issubclass(np_dtype, np.floating):
    try:
      float_list = tf.train.FloatList()
      float_list.value.append(np.float32(0.1))       # Any dummy value will do.
      float_list.value.append(np.array(0.1))         # Any dummy value will do.
      float_list.value.extend(np.array([0.1, 0.2]))  # Any dummy values will do.
      return identity
    except TypeError:
      return numeric_cast
  elif issubclass(np_dtype, np.integer):
    try:
      int64_list = tf.train.Int64List()
      int64_list.value.append(np.int64(1))       # Any dummy value will do.
      int64_list.value.append(np.array(1))       # Any dummy value will do.
      int64_list.value.extend(np.array([1, 2]))  # Any dummy values will do.
      return identity
    except TypeError:
      return numeric_cast
  else:
    return string_cast


def _make_feature_value_fn(dtype):
  """Return a function to extract the typed value from the feature.

  For performance reasons it is preferred to have the feature value fn
  constructed once (for each handler).

  Args:
    dtype: The type of the Tensorflow feature.
  Returns:
    A function to extract the value field from the feature depending on dtype.
  """
  if dtype.is_integer:
    return lambda feature: feature.int64_list.value
  elif dtype.is_floating:
    return lambda feature: feature.float_list.value
  else:
    return lambda feature: feature.bytes_list.value


class _FixedLenFeatureHandler(object):
  """Handler for `FixedLenFeature` values.

  `FixedLenFeature` values will be parsed to a list of the corresponding
  dtype.
  """

  def __init__(self, name, feature_spec):
    self._name = name
    self._np_dtype = feature_spec.dtype.as_numpy_dtype
    self._value_fn = _make_feature_value_fn(feature_spec.dtype)
    self._shape = feature_spec.shape
    self._rank = len(feature_spec.shape)
    self._size = 1
    for dim in feature_spec.shape:
      self._size *= dim
    default_value = feature_spec.default_value
    if default_value is not None:
      try:
        np_default_value = np.asarray(default_value, dtype=self._np_dtype)
      except ValueError:
        raise ValueError(
            'FixedLenFeature %r got default value with incompatible dtype %s' %
            (self._name, feature_spec.dtype))
      if list(np_default_value.shape) != self._shape:
        raise ValueError(
            'FixedLenFeature %r got default value with incorrect shape' %
            self._name)
      default_value = np_default_value.reshape(-1).tolist()
    self._default_value = default_value

  @property
  def name(self):
    """The name of the feature."""
    return self._name

  def initialize_encode_cache(self, example):
    """Initialize fields (performance caches) that point to example's state."""
    self._cast_fn = _make_cast_fn(self._np_dtype)
    self._value = self._value_fn(example.features.feature[self._name])

  def parse_value(self, feature_map):
    """Non-Mutating Decode of a feature into its TF.Transform representation."""
    if self._name in feature_map:
      feature = feature_map[self._name]
      if feature.WhichOneof('kind') is None:
        values = self._default_value
      else:
        values = self._value_fn(feature)
    elif self._default_value is not None:
      values = self._default_value
    else:
      values = []

    if len(values) != self._size:
      raise ValueError('FixedLenFeature %r got wrong number of values. Expected'
                       ' %d but got %d' % (self._name, self._size, len(values)))

    if self._rank == 0:
      # Encode the values as a scalar if shape == [].
      return values[0]
    elif self._rank == 1:
      # Short-circuit the reshaping logic needed for rank > 1.
      return np.asarray(values, dtype=self._np_dtype)
    else:
      return np.asarray(values, dtype=self._np_dtype).reshape(self._shape)

  def encode_value(self, values):
    """Encodes a feature into its Example proto representation."""
    del self._value[:]
    if self._rank == 0:
      self._value.append(self._cast_fn(values))
    else:
      flattened_values = (
          values if self._rank == 1 else np.asarray(
              values, dtype=self._np_dtype).reshape(-1))
      if len(flattened_values) != self._size:
        raise ValueError('FixedLenFeature %r got wrong number of values. '
                         'Expected %d but got %d' %
                         (self._name, self._size, len(flattened_values)))
      self._value.extend(self._cast_fn(flattened_values))


class _VarLenFeatureHandler(object):
  """Handler for `VarLenFeature` values.

  `VarLenFeature` values will be parsed as an array of the corresponding dtype.
  """

  def __init__(self, name, dtype):
    self._name = name
    self._np_dtype = dtype.as_numpy_dtype
    self._value_fn = _make_feature_value_fn(dtype)

  @property
  def name(self):
    """The name of the feature."""
    return self._name

  def initialize_encode_cache(self, example):
    """Initialize fields (performance caches) that point to example's state."""
    self._cast_fn = _make_cast_fn(self._np_dtype)
    self._feature = example.features.feature[self._name]
    self._value = self._value_fn(self._feature)

  def parse_value(self, feature_map):
    """Non-Mutating Decode of a feature into its TF.Transform representation."""
    if self._name in feature_map:
      feature = feature_map[self._name]
      if feature.WhichOneof('kind') is None:
        return None
      else:
        return list(self._value_fn(feature))
    else:
      return None

  def encode_value(self, values):
    if values is None:
      self._feature.Clear()
      # Note after Clear(), self._value no longer points to a submessage of
      # self._feature so we need to reset it.
      self._value = self._value_fn(self._feature)
    else:
      del self._value[:]
      self._value.extend(self._cast_fn(values))


class ExampleProtoCoder(object):
  """A coder between maybe-serialized TF Examples and tf.Transform datasets."""

  def __init__(self, schema, serialized=True):
    """Build an ExampleProtoCoder.

    Args:
      schema: A `Schema` proto.
      serialized: Whether to encode / decode serialized Example protos (as
        opposed to in-memory Example protos). The default (True) is used for
        backwards compatibility. Note that the serialized=True option might be
        removed in a future version.
    Raises:
      ValueError: If `schema` is invalid.
    """
    self._schema = schema
    self._serialized = serialized

    # Using pre-allocated tf.train.Example and FeatureHandler objects for
    # performance reasons.
    #
    # The _encode_example_cache is used solely by "encode" paths while the
    # the _decode_example_cache is used solely be "decode" paths, since the
    # caching strategies are incompatible with each other (due to proto
    # parsing/merging implementation).
    #
    # Since the output of both "encode" and "decode" are deep as opposed to
    # shallow transformations, and since the schema always fully defines the
    # Example's FeatureMap (ie all fields are always cleared/assigned or
    # copied), the optimizations and implementation are correct and
    # thread-compatible.
    self._encode_example_cache = tf.train.Example()
    self._decode_example_cache = tf.train.Example()
    self._feature_handlers = []
    for name, feature_spec in six.iteritems(
        schema_utils.schema_as_feature_spec(schema).feature_spec):
      if isinstance(feature_spec, tf.io.FixedLenFeature):
        self._feature_handlers.append(
            _FixedLenFeatureHandler(name, feature_spec))
      elif isinstance(feature_spec, tf.io.VarLenFeature):
        self._feature_handlers.append(
            _VarLenFeatureHandler(name, feature_spec.dtype))
      elif isinstance(feature_spec, tf.io.SparseFeature):
        self._feature_handlers.append(
            _VarLenFeatureHandler(feature_spec.index_key, tf.int64))
        self._feature_handlers.append(
            _VarLenFeatureHandler(feature_spec.value_key, feature_spec.dtype))
      else:
        raise ValueError('feature_spec should be one of tf.FixedLenFeature, '
                         'tf.VarLenFeature or tf.SparseFeature: %s was %s' %
                         (name, type(feature_spec)))

    for feature_handler in self._feature_handlers:
      feature_handler.initialize_encode_cache(self._encode_example_cache)

  def __reduce__(self):
    return ExampleProtoCoder, (self._schema, self._serialized)

  def encode(self, instance):
    """Encode a tf.transform encoded dict as tf.Example."""
    # The feature handles encode using the self._encode_example_cache.
    for feature_handler in self._feature_handlers:
      value = instance[feature_handler.name]
      try:
        feature_handler.encode_value(value)
      except TypeError as e:
        raise TypeError('%s while encoding feature "%s"' %
                        (e, feature_handler.name))

    if self._serialized:
      return self._encode_example_cache.SerializeToString()
    else:
      result = tf.train.Example()
      result.CopyFrom(self._encode_example_cache)
      return result

  def decode(self, example_proto):
    """Decode tf.Example as a tf.transform encoded dict."""
    if self._serialized:
      example = self._decode_example_cache
      example.ParseFromString(example_proto)
    else:
      example = example_proto

    feature_map = example.features.feature
    return {feature_handler.name: feature_handler.parse_value(feature_map)
            for feature_handler in self._feature_handlers}
