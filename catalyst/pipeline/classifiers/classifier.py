"""
classifier.py
"""
from numbers import Number
import operator
import re

from numpy import where, isnan, nan, zeros
import pandas as pd

from catalyst.lib.labelarray import LabelArray
from catalyst.lib.quantiles import quantiles
from catalyst.pipeline.api_utils import restrict_to_dtype
from catalyst.pipeline.sentinels import NotSpecified
from catalyst.pipeline.term import ComputableTerm
from catalyst.utils.compat import unicode
from catalyst.utils.input_validation import expect_types, expect_dtypes
from catalyst.utils.memoize import classlazyval
from catalyst.utils.numpy_utils import (
    categorical_dtype,
    int64_dtype,
    vectorized_is_element,
)

from ..filters import ArrayPredicate, NotNullFilter, NullFilter, NumExprFilter
from ..mixins import (
    AliasedMixin,
    CustomTermMixin,
    DownsampledMixin,
    LatestMixin,
    PositiveWindowLengthMixin,
    RestrictedDTypeMixin,
    SingleInputMixin,
    StandardOutputs,
)


string_classifiers_only = restrict_to_dtype(
    dtype=categorical_dtype,
    message_template=(
        "{method_name}() is only defined on Classifiers producing strings"
        " but it was called on a Classifier of dtype {received_dtype}."
    )
)


class Classifier(RestrictedDTypeMixin, ComputableTerm):
    """
    A Pipeline expression computing a categorical output.

    Classifiers are most commonly useful for describing grouping keys for
    complex transformations on Factor outputs. For example, Factor.demean() and
    Factor.zscore() can be passed a Classifier in their ``groupby`` argument,
    indicating that means/standard deviations should be computed on assets for
    which the classifier produced the same label.
    """
    # Used by RestrictedDTypeMixin
    ALLOWED_DTYPES = (int64_dtype, categorical_dtype)
    categories = NotSpecified

    def isnull(self):
        """
        A Filter producing True for values where this term has missing data.
        """
        return NullFilter(self)

    def notnull(self):
        """
        A Filter producing True for values where this term has complete data.
        """
        return NotNullFilter(self)

    # We explicitly don't support classifier to classifier comparisons, since
    # the stored values likely don't mean the same thing. This may be relaxed
    # in the future, but for now we're starting conservatively.
    def eq(self, other):
        """
        Construct a Filter returning True for asset/date pairs where the output
        of ``self`` matches ``other``.
        """
        # We treat this as an error because missing_values have NaN semantics,
        # which means this would return an array of all False, which is almost
        # certainly not what the user wants.
        if other == self.missing_value:
            raise ValueError(
                "Comparison against self.missing_value ({value!r}) in"
                " {typename}.eq().\n"
                "Missing values have NaN semantics, so the "
                "requested comparison would always produce False.\n"
                "Use the isnull() method to check for missing values.".format(
                    value=other,
                    typename=(type(self).__name__),
                )
            )

        if isinstance(other, Number) != (self.dtype == int64_dtype):
            raise InvalidClassifierComparison(self, other)

        if isinstance(other, Number):
            return NumExprFilter.create(
                "x_0 == {other}".format(other=int(other)),
                binds=(self,),
            )
        else:
            return ArrayPredicate(
                term=self,
                op=operator.eq,
                opargs=(other,),
            )

    def __ne__(self, other):
        """
        Construct a Filter returning True for asset/date pairs where the output
        of ``self`` matches ``other.
        """
        if isinstance(other, Number) != (self.dtype == int64_dtype):
            raise InvalidClassifierComparison(self, other)

        if isinstance(other, Number):
            return NumExprFilter.create(
                "((x_0 != {other}) & (x_0 != {missing}))".format(
                    other=int(other),
                    missing=self.missing_value,
                ),
                binds=(self,),
            )
        else:
            # Numexpr doesn't know how to use LabelArrays.
            return ArrayPredicate(term=self, op=operator.ne, opargs=(other,))

    @string_classifiers_only
    @expect_types(prefix=(bytes, unicode))
    def startswith(self, prefix):
        """
        Construct a Filter matching values starting with ``prefix``.

        Parameters
        ----------
        prefix : str
            String prefix against which to compare values produced by ``self``.

        Returns
        -------
        matches : Filter
            Filter returning True for all sid/date pairs for which ``self``
            produces a string starting with ``prefix``.
        """
        return ArrayPredicate(
            term=self,
            op=LabelArray.startswith,
            opargs=(prefix,),
        )

    @string_classifiers_only
    @expect_types(suffix=(bytes, unicode))
    def endswith(self, suffix):
        """
        Construct a Filter matching values ending with ``suffix``.

        Parameters
        ----------
        suffix : str
            String suffix against which to compare values produced by ``self``.

        Returns
        -------
        matches : Filter
            Filter returning True for all sid/date pairs for which ``self``
            produces a string ending with ``prefix``.
        """
        return ArrayPredicate(
            term=self,
            op=LabelArray.endswith,
            opargs=(suffix,),
        )

    @string_classifiers_only
    @expect_types(substring=(bytes, unicode))
    def has_substring(self, substring):
        """
        Construct a Filter matching values containing ``substring``.

        Parameters
        ----------
        substring : str
            Sub-string against which to compare values produced by ``self``.

        Returns
        -------
        matches : Filter
            Filter returning True for all sid/date pairs for which ``self``
            produces a string containing ``substring``.
        """
        return ArrayPredicate(
            term=self,
            op=LabelArray.has_substring,
            opargs=(substring,),
        )

    @string_classifiers_only
    @expect_types(pattern=(bytes, unicode, type(re.compile(''))))
    def matches(self, pattern):
        """
        Construct a Filter that checks regex matches against ``pattern``.

        Parameters
        ----------
        pattern : str
            Regex pattern against which to compare values produced by ``self``.

        Returns
        -------
        matches : Filter
            Filter returning True for all sid/date pairs for which ``self``
            produces a string matched by ``pattern``.

        See Also
        --------
        :mod:`Python Regular Expressions <re>`
        """
        return ArrayPredicate(
            term=self,
            op=LabelArray.matches,
            opargs=(pattern,),
        )

    # TODO: Support relabeling for integer dtypes.
    @string_classifiers_only
    def relabel(self, relabeler):
        """
        Convert ``self`` into a new classifier by mapping a function over each
        element produced by ``self``.

        Parameters
        ----------
        relabeler : function[str -> str or None]
            A function to apply to each unique value produced by ``self``.

        Returns
        -------
        relabeled : Classifier
            A classifier produced by applying ``relabeler`` to each unique
            value produced by ``self``.
        """
        return Relabel(term=self, relabeler=relabeler)

    def element_of(self, choices):
        """
        Construct a Filter indicating whether values are in ``choices``.

        Parameters
        ----------
        choices : iterable[str or int]
            An iterable of choices.

        Returns
        -------
        matches : Filter
            Filter returning True for all sid/date pairs for which ``self``
            produces an entry in ``choices``.
        """
        try:
            choices = frozenset(choices)
        except Exception as e:
            raise TypeError(
                "Expected `choices` to be an iterable of hashable values,"
                " but got {} instead.\n"
                "This caused the following error: {!r}.".format(choices, e)
            )

        if self.missing_value in choices:
            raise ValueError(
                "Found self.missing_value ({mv!r}) in choices supplied to"
                " {typename}.{meth_name}().\n"
                "Missing values have NaN semantics, so the"
                " requested comparison would always produce False.\n"
                "Use the isnull() method to check for missing values.\n"
                "Received choices were {choices}.".format(
                    mv=self.missing_value,
                    typename=(type(self).__name__),
                    choices=sorted(choices),
                    meth_name=self.element_of.__name__,
                )
            )

        def only_contains(type_, values):
            return all(isinstance(v, type_) for v in values)

        if self.dtype == int64_dtype:
            if only_contains(int, choices):
                return ArrayPredicate(
                    term=self,
                    op=vectorized_is_element,
                    opargs=(choices,),
                )
            else:
                raise TypeError(
                    "Found non-int in choices for {typename}.element_of.\n"
                    "Supplied choices were {choices}.".format(
                        typename=type(self).__name__,
                        choices=choices,
                    )
                )
        elif self.dtype == categorical_dtype:
            if only_contains((bytes, unicode), choices):
                return ArrayPredicate(
                    term=self,
                    op=LabelArray.element_of,
                    opargs=(choices,),
                )
            else:
                raise TypeError(
                    "Found non-string in choices for {typename}.element_of.\n"
                    "Supplied choices were {choices}.".format(
                        typename=type(self).__name__,
                        choices=choices,
                    )
                )
        assert False, "Unknown dtype in Classifier.element_of %s." % self.dtype

    def postprocess(self, data):
        if self.dtype == int64_dtype:
            return data
        if not isinstance(data, LabelArray):
            raise AssertionError("Expected a LabelArray, got %s." % type(data))
        return data.as_categorical()

    def to_workspace_value(self, result, assets):
        """
        Called with the result of a pipeline. This needs to return an object
        which can be put into the workspace to continue doing computations.

        This is the inverse of
        :func:`~catalyst.pipeline.term.Term.postprocess`.
        """
        if self.dtype == int64_dtype:
            return super(Classifier, self).to_workspace_value(result, assets)

        assert isinstance(result.values, pd.Categorical), (
            'Expected a Categorical, got %r.' % type(result.values)
        )
        with_missing = pd.Series(
            data=pd.Categorical(
                result.values,
                result.values.categories.union([self.missing_value]),
            ),
            index=result.index,
        )
        return LabelArray(
            super(Classifier, self).to_workspace_value(
                with_missing,
                assets,
            ),
            self.missing_value,
        )

    @classlazyval
    def _downsampled_type(self):
        return DownsampledMixin.make_downsampled_type(Classifier)

    @classlazyval
    def _aliased_type(self):
        return AliasedMixin.make_aliased_type(Classifier)


class Everything(Classifier):
    """
    A trivial classifier that classifies everything the same.
    """
    dtype = int64_dtype
    window_length = 0
    inputs = ()
    missing_value = -1

    def _compute(self, arrays, dates, assets, mask):
        return where(
            mask,
            zeros(shape=mask.shape, dtype=int64_dtype),
            self.missing_value,
        )


class Quantiles(SingleInputMixin, Classifier):
    """
    A classifier computing quantiles over an input.
    """
    params = ('bins',)
    dtype = int64_dtype
    window_length = 0
    missing_value = -1

    def _compute(self, arrays, dates, assets, mask):
        data = arrays[0]
        bins = self.params['bins']
        to_bin = where(mask, data, nan)
        result = quantiles(to_bin, bins)
        # Write self.missing_value into nan locations, whether they were
        # generated by our input mask or not.
        result[isnan(result)] = self.missing_value
        return result.astype(int64_dtype)

    def short_repr(self):
        return type(self).__name__ + '(%d)' % self.params['bins']


class Relabel(SingleInputMixin, Classifier):
    """
    A classifier applying a relabeling function on the result of another
    classifier.

    Parameters
    ----------
    arg : catalyst.pipeline.Classifier
        Term produceing the input to be relabeled.
    relabel_func : function(LabelArray) -> LabelArray
        Function to apply to the result of `term`.
    """
    window_length = 0
    params = ('relabeler',)

    # TODO: Support relabeling for integer dtypes.
    @expect_dtypes(term=categorical_dtype)
    @expect_types(term=Classifier)
    def __new__(cls, term, relabeler):
        return super(Relabel, cls).__new__(
            cls,
            inputs=(term,),
            dtype=term.dtype,
            mask=term.mask,
            relabeler=relabeler,
        )

    def _compute(self, arrays, dates, assets, mask):
        relabeler = self.params['relabeler']
        data = arrays[0]

        if isinstance(data, LabelArray):
            result = data.map(relabeler)
            result[~mask] = data.missing_value
        else:
            raise NotImplementedError(
                "Relabeling is not currently supported for "
                "int-dtype classifiers."
            )
        return result


class CustomClassifier(PositiveWindowLengthMixin,
                       StandardOutputs,
                       CustomTermMixin,
                       Classifier):
    """
    Base class for user-defined Classifiers.

    Does not suppport multiple outputs.

    See Also
    --------
    catalyst.pipeline.CustomFactor
    catalyst.pipeline.CustomFilter
    """
    def _allocate_output(self, windows, shape):
        """
        Override the default array allocation to produce a LabelArray when we
        have a string-like dtype.
        """
        if self.dtype == int64_dtype:
            return super(CustomClassifier, self)._allocate_output(
                windows,
                shape,
            )

        # This is a little bit of a hack.  We might not know what the
        # categories for a LabelArray are until it's actually been loaded, so
        # we need to look at the underlying data.
        return windows[0].data.empty_like(shape)


class Latest(LatestMixin, CustomClassifier):
    """
    A classifier producing the latest value of an input.

    See Also
    --------
    catalyst.pipeline.data.dataset.BoundColumn.latest
    catalyst.pipeline.factors.factor.Latest
    catalyst.pipeline.filters.filter.Latest
    """
    pass


class InvalidClassifierComparison(TypeError):
    def __init__(self, classifier, compval):
        super(InvalidClassifierComparison, self).__init__(
            "Can't compare classifier of dtype"
            " {dtype} to value {value} of type {type}.".format(
                dtype=classifier.dtype,
                value=compval,
                type=type(compval).__name__,
            )
        )
