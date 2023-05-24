# Licensed to Modin Development Team under one or more contributor license agreements.
# See the NOTICE file distributed with this work for additional information regarding
# copyright ownership.  The Modin Development Team licenses this file to you under the
# Apache License, Version 2.0 (the "License"); you may not use this file except in
# compliance with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under
# the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific language
# governing permissions and limitations under the License.

import pytest
import itertools
import pandas
import numpy as np
from unittest import mock
import datetime

from modin.config import StorageFormat
from modin.config.envvars import Engine, ExperimentalGroupbyImpl
from modin.core.dataframe.pandas.partitioning.axis_partition import (
    PandasDataframeAxisPartition,
)
import modin.pandas as pd
from modin.utils import (
    try_cast_to_pandas,
    get_current_execution,
    hashable,
    MODIN_UNNAMED_SERIES_LABEL,
)
from modin.core.dataframe.algebra.default2pandas.groupby import GroupBy
from modin.pandas.utils import from_pandas, is_scalar
from .utils import (
    df_equals,
    check_df_columns_have_nans,
    create_test_dfs,
    eval_general,
    test_data,
    test_data_values,
    modin_df_almost_equals_pandas,
    try_modin_df_almost_equals_compare,
    generate_multiindex,
    test_groupby_data,
    dict_equals,
    value_equals,
    default_to_pandas_ignore_string,
)
from modin.config import NPartitions
from modin.test.test_utils import warns_that_defaulting_to_pandas


NPartitions.put(4)

# Our configuration in pytest.ini requires that we explicitly catch all
# instances of defaulting to pandas, but some test modules, like this one,
# have too many such instances.
# TODO(https://github.com/modin-project/modin/issues/3655): catch all instances
# of defaulting to pandas.
pytestmark = pytest.mark.filterwarnings(default_to_pandas_ignore_string)


def modin_groupby_equals_pandas(modin_groupby, pandas_groupby):
    eval_general(
        modin_groupby, pandas_groupby, lambda grp: grp.indices, comparator=dict_equals
    )
    eval_general(
        modin_groupby, pandas_groupby, lambda grp: grp.groups, comparator=dict_equals
    )

    for g1, g2 in itertools.zip_longest(modin_groupby, pandas_groupby):
        value_equals(g1[0], g2[0])
        df_equals(g1[1], g2[1])


def eval_aggregation(md_df, pd_df, operation=None, by=None, *args, **kwargs):
    if by is None:
        by = md_df.columns[0]
    if operation is None:
        operation = {}
    return eval_general(
        md_df,
        pd_df,
        lambda df, *args, **kwargs: df.groupby(by=by).agg(operation, *args, **kwargs),
        *args,
        **kwargs,
    )


def build_types_asserter(comparator):
    def wrapper(obj1, obj2, *args, **kwargs):
        error_str = f"obj1 and obj2 has incorrect types: {type(obj1)} and {type(obj2)}"
        assert not (is_scalar(obj1) ^ is_scalar(obj2)), error_str
        assert obj1.__module__.split(".")[0] == "modin", error_str
        assert obj2.__module__.split(".")[0] == "pandas", error_str
        comparator(obj1, obj2, *args, **kwargs)

    return wrapper


@pytest.mark.parametrize("as_index", [True, False])
def test_mixed_dtypes_groupby(as_index):
    frame_data = np.random.RandomState(42).randint(97, 198, size=(2**6, 2**4))
    pandas_df = pandas.DataFrame(frame_data).add_prefix("col")
    # Convert every other column to string
    for col in pandas_df.iloc[
        :, [i for i in range(len(pandas_df.columns)) if i % 2 == 0]
    ]:
        pandas_df[col] = [str(chr(i)) for i in pandas_df[col]]
    modin_df = from_pandas(pandas_df)

    n = 1

    by_values = [
        ("col1",),
        (lambda x: x % 2,),
        (modin_df["col0"].copy(), pandas_df["col0"].copy()),
        ("col3",),
    ]

    for by in by_values:
        if isinstance(by[0], str) and by[0] == "col3":
            modin_groupby = modin_df.set_index(by[0]).groupby(
                by=by[0], as_index=as_index
            )
            pandas_groupby = pandas_df.set_index(by[0]).groupby(
                by=by[-1], as_index=as_index
            )
            # difference in behaviour between .groupby().ffill() and
            # .groupby.fillna(method='ffill') on duplicated indices
            # caused by https://github.com/pandas-dev/pandas/issues/43412
            # is hurting the tests, for now sort the frames
            md_sorted_grpby = (
                modin_df.set_index(by[0])
                .sort_index()
                .groupby(by=by[0], as_index=as_index)
            )
            pd_sorted_grpby = (
                pandas_df.set_index(by[0])
                .sort_index()
                .groupby(by=by[0], as_index=as_index)
            )
        else:
            modin_groupby = modin_df.groupby(by=by[0], as_index=as_index)
            pandas_groupby = pandas_df.groupby(by=by[-1], as_index=as_index)
            md_sorted_grpby, pd_sorted_grpby = modin_groupby, pandas_groupby

        modin_groupby_equals_pandas(modin_groupby, pandas_groupby)
        eval_ngroups(modin_groupby, pandas_groupby)
        eval_general(
            md_sorted_grpby,
            pd_sorted_grpby,
            lambda df: df.ffill(),
            comparator=lambda *dfs: df_equals(
                *sort_index_if_experimental_groupby(*dfs)
            ),
        )
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.sem(),
            modin_df_almost_equals_pandas,
        )
        eval_general(
            modin_groupby, pandas_groupby, lambda df: df.sample(random_state=1)
        )
        eval_general(modin_groupby, pandas_groupby, lambda df: df.ewm(com=0.5).std())
        eval_shift(modin_groupby, pandas_groupby)
        eval_mean(modin_groupby, pandas_groupby)
        eval_any(modin_groupby, pandas_groupby)
        eval_min(modin_groupby, pandas_groupby)
        eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmax())
        eval_ndim(modin_groupby, pandas_groupby)
        eval_cumsum(modin_groupby, pandas_groupby)
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.pct_change(),
            modin_df_almost_equals_pandas,
        )
        eval_cummax(modin_groupby, pandas_groupby)

        # TODO Add more apply functions
        apply_functions = [lambda df: df.sum(), min]
        for func in apply_functions:
            eval_apply(modin_groupby, pandas_groupby, func)

        eval_dtypes(modin_groupby, pandas_groupby)
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.first(),
            comparator=lambda *dfs: df_equals(
                *sort_index_if_experimental_groupby(*dfs)
            ),
        )
        eval_general(
            md_sorted_grpby,
            pd_sorted_grpby,
            lambda df: df.backfill(),
            comparator=lambda *dfs: df_equals(
                *sort_index_if_experimental_groupby(*dfs)
            ),
        )
        eval_cummin(modin_groupby, pandas_groupby)
        eval_general(
            md_sorted_grpby,
            pd_sorted_grpby,
            lambda df: df.bfill(),
            comparator=lambda *dfs: df_equals(
                *sort_index_if_experimental_groupby(*dfs)
            ),
        )
        eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmin())
        eval_prod(modin_groupby, pandas_groupby)
        if as_index:
            eval_std(modin_groupby, pandas_groupby)
            eval_var(modin_groupby, pandas_groupby)
            eval_skew(modin_groupby, pandas_groupby)

        agg_functions = [
            lambda df: df.sum(),
            "min",
            min,
            "max",
            max,
            sum,
            {"col2": "sum"},
            {"col2": sum},
            {"col2": "max", "col4": "sum", "col5": "min"},
            {"col2": max, "col4": sum, "col5": "min"},
            # Intersection of 'by' and agg cols for TreeReduce impl
            {"col0": "count", "col1": "count", "col2": "count"},
            # Intersection of 'by' and agg cols for FullAxis impl
            {"col0": "nunique", "col1": "nunique", "col2": "nunique"},
        ]
        for func in agg_functions:
            eval_agg(modin_groupby, pandas_groupby, func)
            eval_aggregate(modin_groupby, pandas_groupby, func)

        eval_general(modin_groupby, pandas_groupby, lambda df: df.last())
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.mad(),
            modin_df_almost_equals_pandas,
        )
        eval_max(modin_groupby, pandas_groupby)
        eval_len(modin_groupby, pandas_groupby)
        eval_sum(modin_groupby, pandas_groupby)
        if not ExperimentalGroupbyImpl.get():
            # `.group` fails with experimental groupby
            # https://github.com/modin-project/modin/issues/6083
            eval_ngroup(modin_groupby, pandas_groupby)
        eval_nunique(modin_groupby, pandas_groupby)
        eval_value_counts(modin_groupby, pandas_groupby)
        eval_median(modin_groupby, pandas_groupby)
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.head(n),
            comparator=lambda *dfs: df_equals(
                *sort_index_if_experimental_groupby(*dfs)
            ),
        )
        eval_cumprod(modin_groupby, pandas_groupby)
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.cov(),
            modin_df_almost_equals_pandas,
        )

        transform_functions = [lambda df: df, lambda df: df + df]
        for func in transform_functions:
            eval_transform(modin_groupby, pandas_groupby, func)

        pipe_functions = [lambda dfgb: dfgb.sum()]
        for func in pipe_functions:
            eval_pipe(modin_groupby, pandas_groupby, func)

        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.corr(),
            modin_df_almost_equals_pandas,
        )
        eval_fillna(modin_groupby, pandas_groupby)
        eval_count(modin_groupby, pandas_groupby)
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.tail(n),
            comparator=lambda *dfs: df_equals(
                *sort_index_if_experimental_groupby(*dfs)
            ),
        )
        eval_quantile(modin_groupby, pandas_groupby)
        eval_general(modin_groupby, pandas_groupby, lambda df: df.take([0]))
        eval___getattr__(modin_groupby, pandas_groupby, "col2")
        eval_groups(modin_groupby, pandas_groupby)


class GetColumn:
    """Indicate to the test that it should do gc(df)."""

    def __init__(self, name):
        self.name = name

    def __call__(self, df):
        return df[self.name]


@pytest.mark.parametrize(
    "by",
    [
        [1, 2, 1, 2],
        lambda x: x % 3,
        "col1",
        ["col1"],
        # col2 contains NaN, is it necessary to test functions like size()
        "col2",
        ["col2"],  # 5
        pytest.param(
            ["col1", "col2"],
            marks=pytest.mark.xfail(reason="Excluded because of bug #1554"),
        ),
        pytest.param(
            ["col2", "col4"],
            marks=pytest.mark.xfail(reason="Excluded because of bug #1554"),
        ),
        pytest.param(
            ["col4", "col2"],
            marks=pytest.mark.xfail(reason="Excluded because of bug #1554"),
        ),
        pytest.param(
            ["col3", "col4", "col2"],
            marks=pytest.mark.xfail(reason="Excluded because of bug #1554"),
        ),
        # but cum* functions produce undefined results with NaNs so we need to test the same combinations without NaN too
        ["col5"],  # 10
        ["col1", "col5"],
        ["col5", "col4"],
        ["col4", "col5"],
        ["col5", "col4", "col1"],
        ["col1", pd.Series([1, 5, 7, 8])],  # 15
        [pd.Series([1, 5, 7, 8])],
        [
            pd.Series([1, 5, 7, 8]),
            pd.Series([1, 5, 7, 8]),
            pd.Series([1, 5, 7, 8]),
            pd.Series([1, 5, 7, 8]),
            pd.Series([1, 5, 7, 8]),
        ],
        ["col1", GetColumn("col5")],
        [GetColumn("col1"), GetColumn("col5")],
        [GetColumn("col1")],  # 20
    ],
)
@pytest.mark.parametrize("as_index", [True, False], ids=lambda v: f"as_index={v}")
@pytest.mark.parametrize(
    "col1_category", [True, False], ids=lambda v: f"col1_category={v}"
)
def test_simple_row_groupby(by, as_index, col1_category):
    pandas_df = pandas.DataFrame(
        {
            "col1": [0, 1, 2, 3],
            "col2": [4, 5, np.NaN, 7],
            "col3": [np.NaN, np.NaN, 12, 10],
            "col4": [17, 13, 16, 15],
            "col5": [-4, -5, -6, -7],
        }
    )

    if col1_category:
        pandas_df = pandas_df.astype({"col1": "category"})
        # As of pandas 1.4.0 operators like min cause TypeErrors to be raised on unordered
        # categorical columns. We need to specify the categorical column as ordered to bypass this.
        pandas_df["col1"] = pandas_df["col1"].cat.as_ordered()

    modin_df = from_pandas(pandas_df)
    n = 1

    def maybe_get_columns(df, by):
        if isinstance(by, list):
            return [o(df) if isinstance(o, GetColumn) else o for o in by]
        else:
            return by

    modin_groupby = modin_df.groupby(
        by=maybe_get_columns(modin_df, by), as_index=as_index
    )

    pandas_by = maybe_get_columns(pandas_df, try_cast_to_pandas(by))
    pandas_groupby = pandas_df.groupby(by=pandas_by, as_index=as_index)

    modin_groupby_equals_pandas(modin_groupby, pandas_groupby)
    eval_ngroups(modin_groupby, pandas_groupby)
    eval_shift(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.ffill())
    eval_general(modin_groupby, pandas_groupby, lambda df: df.pad())
    if as_index:
        eval_general(modin_groupby, pandas_groupby, lambda df: df.nth(0))
    else:
        # FIXME: df.groupby(as_index=False).nth() does not produce correct index in Modin,
        #        it should maintain values from df.index, not create a new one or re-order it;
        #        it also produces completely wrong result for multi-column `by` :(
        if not isinstance(pandas_by, list) or len(pandas_by) <= 1:
            eval_general(
                modin_groupby,
                pandas_groupby,
                lambda df: df.nth(0).sort_values("col1").reset_index(drop=True),
            )
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.sem(),
        modin_df_almost_equals_pandas,
    )
    eval_mean(modin_groupby, pandas_groupby)
    eval_any(modin_groupby, pandas_groupby)
    eval_min(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmax())
    eval_ndim(modin_groupby, pandas_groupby)
    if not check_df_columns_have_nans(modin_df, by):
        # cum* functions produce undefined results for columns with NaNs so we run them only when "by" columns contain no NaNs
        eval_general(modin_groupby, pandas_groupby, lambda df: df.cumsum(axis=0))
        eval_general(modin_groupby, pandas_groupby, lambda df: df.cummax(axis=0))
        eval_general(modin_groupby, pandas_groupby, lambda df: df.cummin(axis=0))
        eval_general(modin_groupby, pandas_groupby, lambda df: df.cumprod(axis=0))
        eval_general(modin_groupby, pandas_groupby, lambda df: df.cumcount())

    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.pct_change(
            periods=2, fill_method="pad", limit=1, freq=None, axis=1
        ),
        modin_df_almost_equals_pandas,
    )

    apply_functions = [
        lambda df: df.sum(),
        lambda df: pandas.Series([1, 2, 3, 4], name="result"),
        min,
    ]
    for func in apply_functions:
        eval_apply(modin_groupby, pandas_groupby, func)

    eval_dtypes(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.first())
    eval_general(modin_groupby, pandas_groupby, lambda df: df.backfill())
    eval_general(modin_groupby, pandas_groupby, lambda df: df.bfill())
    eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmin())
    eval_prod(modin_groupby, pandas_groupby)
    if as_index:
        eval_std(modin_groupby, pandas_groupby)
        eval_var(modin_groupby, pandas_groupby)
        eval_skew(modin_groupby, pandas_groupby)

    agg_functions = [
        lambda df: df.sum(),
        "min",
        "max",
        min,
        sum,
        # Intersection of 'by' and agg cols for TreeReduce impl
        {"col1": "count", "col2": "count"},
        # Intersection of 'by' and agg cols for FullAxis impl
        {"col1": "nunique", "col2": "nunique"},
    ]
    for func in agg_functions:
        # Pandas raises an exception when 'by' contains categorical key and `as_index=False`
        # because of this bug: https://github.com/pandas-dev/pandas/issues/36698
        # Modin correctly processes the result, that's why `check_exception_type=None` in some cases
        is_pandas_bug_case = not as_index and col1_category and isinstance(func, dict)

        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda grp: grp.agg(func),
            check_exception_type=None if is_pandas_bug_case else True,
        )
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda grp: grp.aggregate(func),
            check_exception_type=None if is_pandas_bug_case else True,
        )

    eval_general(modin_groupby, pandas_groupby, lambda df: df.last())
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.mad(),
        modin_df_almost_equals_pandas,
    )
    eval_general(modin_groupby, pandas_groupby, lambda df: df.rank())
    eval_max(modin_groupby, pandas_groupby)
    eval_len(modin_groupby, pandas_groupby)
    eval_sum(modin_groupby, pandas_groupby)
    eval_ngroup(modin_groupby, pandas_groupby)
    # Pandas raising exception when 'by' contains categorical key and `as_index=False`
    # because of a bug: https://github.com/pandas-dev/pandas/issues/36698
    # Modin correctly processes the result, so that's why `check_exception_type=None` in some cases
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.nunique(),
        check_exception_type=None if (col1_category and not as_index) else True,
    )
    eval_median(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.head(n))
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.cov(),
        modin_df_almost_equals_pandas,
    )

    if not check_df_columns_have_nans(modin_df, by):
        # Pandas groupby.transform does not work correctly with NaN values in grouping columns. See Pandas bug 17093.
        transform_functions = [lambda df: df + 4, lambda df: -df - 10]
        for func in transform_functions:
            eval_general(
                modin_groupby,
                pandas_groupby,
                lambda df: df.transform(func),
                check_exception_type=None,
            )

    pipe_functions = [lambda dfgb: dfgb.sum()]
    for func in pipe_functions:
        eval_pipe(modin_groupby, pandas_groupby, func)

    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.corr(),
        modin_df_almost_equals_pandas,
    )
    eval_fillna(modin_groupby, pandas_groupby)
    eval_count(modin_groupby, pandas_groupby)
    if get_current_execution() != "BaseOnPython":
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.size(),
            check_exception_type=None,
        )
    eval_general(modin_groupby, pandas_groupby, lambda df: df.tail(n))
    eval_quantile(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.take([0]))
    if isinstance(by, list) and not any(
        isinstance(o, (pd.Series, pandas.Series)) for o in by
    ):
        # Not yet supported for non-original-column-from-dataframe Series in by:
        eval___getattr__(modin_groupby, pandas_groupby, "col3")
        eval___getitem__(modin_groupby, pandas_groupby, "col3")
    eval_groups(modin_groupby, pandas_groupby)
    # Intersection of the selection and 'by' columns is not yet supported
    non_by_cols = (
        # Potential selection starts only from the second column, because the first may
        # be categorical in this test, which is not yet supported
        [col for col in pandas_df.columns[1:] if col not in modin_groupby._internal_by]
        if isinstance(by, list)
        else ["col3", "col4"]
    )
    eval___getitem__(modin_groupby, pandas_groupby, non_by_cols)
    # When GroupBy.__getitem__ meets an intersection of the selection and 'by' columns
    # it throws a warning with the suggested workaround. The following code tests
    # that this workaround works as expected.
    if len(modin_groupby._internal_by) != 0:
        if not isinstance(by, list):
            by = [by]
        by_from_workaround = [
            modin_df[getattr(col, "name", col)].copy()
            if (hashable(col) and col in modin_groupby._internal_by)
            or isinstance(col, GetColumn)
            else col
            for col in by
        ]
        # GroupBy result with 'as_index=False' depends on the 'by' origin, since we forcibly changed
        # the origin of 'by' for modin by doing a copy, set 'as_index=True' to compare results.
        modin_groupby = modin_df.groupby(
            maybe_get_columns(modin_df, by_from_workaround), as_index=True
        )
        pandas_groupby = pandas_df.groupby(pandas_by, as_index=True)
        eval___getitem__(
            modin_groupby,
            pandas_groupby,
            list(modin_groupby._internal_by) + non_by_cols[:1],
        )


def test_single_group_row_groupby():
    pandas_df = pandas.DataFrame(
        {
            "col1": [0, 1, 2, 3],
            "col2": [4, 5, 36, 7],
            "col3": [3, 8, 12, 10],
            "col4": [17, 3, 16, 15],
            "col5": [-4, 5, -6, -7],
        }
    )

    modin_df = from_pandas(pandas_df)

    by = ["1", "1", "1", "1"]
    n = 6

    modin_groupby = modin_df.groupby(by=by)
    pandas_groupby = pandas_df.groupby(by=by)

    modin_groupby_equals_pandas(modin_groupby, pandas_groupby)
    eval_ngroups(modin_groupby, pandas_groupby)
    eval_shift(modin_groupby, pandas_groupby)
    eval_skew(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.ffill())
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.sem(),
        modin_df_almost_equals_pandas,
    )
    eval_mean(modin_groupby, pandas_groupby)
    eval_any(modin_groupby, pandas_groupby)
    eval_min(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmax())
    eval_ndim(modin_groupby, pandas_groupby)
    eval_cumsum(modin_groupby, pandas_groupby)
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.pct_change(),
        modin_df_almost_equals_pandas,
    )
    eval_cummax(modin_groupby, pandas_groupby)

    apply_functions = [lambda df: df.sum(), lambda df: -df]
    for func in apply_functions:
        eval_apply(modin_groupby, pandas_groupby, func)

    eval_dtypes(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.first())
    eval_general(modin_groupby, pandas_groupby, lambda df: df.backfill())
    eval_cummin(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.bfill())
    eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmin())
    eval_prod(modin_groupby, pandas_groupby)
    eval_std(modin_groupby, pandas_groupby)

    agg_functions = [
        lambda df: df.sum(),
        "min",
        "max",
        max,
        sum,
        {"col2": "sum"},
        {"col2": "max", "col4": "sum", "col5": "min"},
    ]
    for func in agg_functions:
        eval_agg(modin_groupby, pandas_groupby, func)
        eval_aggregate(modin_groupby, pandas_groupby, func)

    eval_general(modin_groupby, pandas_groupby, lambda df: df.last())
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.mad(),
        modin_df_almost_equals_pandas,
    )
    eval_rank(modin_groupby, pandas_groupby)
    eval_max(modin_groupby, pandas_groupby)
    eval_var(modin_groupby, pandas_groupby)
    eval_len(modin_groupby, pandas_groupby)
    eval_sum(modin_groupby, pandas_groupby)
    eval_ngroup(modin_groupby, pandas_groupby)
    eval_nunique(modin_groupby, pandas_groupby)
    eval_value_counts(modin_groupby, pandas_groupby)
    eval_median(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.head(n))
    eval_cumprod(modin_groupby, pandas_groupby)
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.cov(),
        modin_df_almost_equals_pandas,
    )

    transform_functions = [lambda df: df + 4, lambda df: -df - 10]
    for func in transform_functions:
        eval_transform(modin_groupby, pandas_groupby, func)

    pipe_functions = [lambda dfgb: dfgb.sum()]
    for func in pipe_functions:
        eval_pipe(modin_groupby, pandas_groupby, func)

    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.corr(),
        modin_df_almost_equals_pandas,
    )
    eval_fillna(modin_groupby, pandas_groupby)
    eval_count(modin_groupby, pandas_groupby)
    eval_size(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.tail(n))
    eval_quantile(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.take([0]))
    eval___getattr__(modin_groupby, pandas_groupby, "col2")
    eval_groups(modin_groupby, pandas_groupby)


@pytest.mark.parametrize("is_by_category", [True, False])
def test_large_row_groupby(is_by_category):
    pandas_df = pandas.DataFrame(
        np.random.randint(0, 8, size=(100, 4)), columns=list("ABCD")
    )

    modin_df = from_pandas(pandas_df)

    by = [str(i) for i in pandas_df["A"].tolist()]

    if is_by_category:
        by = pandas.Categorical(by)

    n = 4

    modin_groupby = modin_df.groupby(by=by)
    pandas_groupby = pandas_df.groupby(by=by)

    modin_groupby_equals_pandas(modin_groupby, pandas_groupby)
    eval_ngroups(modin_groupby, pandas_groupby)
    eval_shift(modin_groupby, pandas_groupby)
    eval_skew(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.ffill())
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.sem(),
        modin_df_almost_equals_pandas,
    )
    eval_mean(modin_groupby, pandas_groupby)
    eval_any(modin_groupby, pandas_groupby)
    eval_min(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmax())
    eval_ndim(modin_groupby, pandas_groupby)
    eval_cumsum(modin_groupby, pandas_groupby)

    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.diff(periods=2),
    )
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.diff(periods=-1),
    )
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.diff(axis=1),
    )

    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.pct_change(),
        modin_df_almost_equals_pandas,
    )
    eval_cummax(modin_groupby, pandas_groupby)

    apply_functions = [lambda df: df.sum(), lambda df: -df]
    for func in apply_functions:
        eval_apply(modin_groupby, pandas_groupby, func)

    eval_dtypes(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.first())
    eval_general(modin_groupby, pandas_groupby, lambda df: df.backfill())
    eval_cummin(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.bfill())
    eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmin())
    # eval_prod(modin_groupby, pandas_groupby) causes overflows
    eval_std(modin_groupby, pandas_groupby)

    agg_functions = [
        lambda df: df.sum(),
        "min",
        "max",
        min,
        sum,
        {"A": "sum"},
        {"A": lambda df: df.sum()},
        {"A": "max", "B": "sum", "C": "min"},
    ]
    for func in agg_functions:
        eval_agg(modin_groupby, pandas_groupby, func)
        eval_aggregate(modin_groupby, pandas_groupby, func)

    eval_general(modin_groupby, pandas_groupby, lambda df: df.last())
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.mad(),
        modin_df_almost_equals_pandas,
    )
    eval_rank(modin_groupby, pandas_groupby)
    eval_max(modin_groupby, pandas_groupby)
    eval_var(modin_groupby, pandas_groupby)
    eval_len(modin_groupby, pandas_groupby)
    eval_sum(modin_groupby, pandas_groupby)
    eval_ngroup(modin_groupby, pandas_groupby)
    eval_nunique(modin_groupby, pandas_groupby)
    eval_value_counts(modin_groupby, pandas_groupby)
    eval_median(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.head(n))
    # eval_cumprod(modin_groupby, pandas_groupby) causes overflows
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.cov(),
        modin_df_almost_equals_pandas,
    )

    transform_functions = [lambda df: df + 4, lambda df: -df - 10]
    for func in transform_functions:
        eval_transform(modin_groupby, pandas_groupby, func)

    pipe_functions = [lambda dfgb: dfgb.sum()]
    for func in pipe_functions:
        eval_pipe(modin_groupby, pandas_groupby, func)

    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.corr(),
        modin_df_almost_equals_pandas,
    )
    eval_fillna(modin_groupby, pandas_groupby)
    eval_count(modin_groupby, pandas_groupby)
    eval_size(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.tail(n))
    eval_quantile(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.take([0]))
    eval_groups(modin_groupby, pandas_groupby)


def test_simple_col_groupby():
    pandas_df = pandas.DataFrame(
        {
            "col1": [0, 3, 2, 3],
            "col2": [4, 1, 6, 7],
            "col3": [3, 8, 2, 10],
            "col4": [1, 13, 6, 15],
            "col5": [-4, 5, 6, -7],
        }
    )

    modin_df = from_pandas(pandas_df)

    by = [1, 2, 3, 2, 1]

    modin_groupby = modin_df.groupby(axis=1, by=by)
    pandas_groupby = pandas_df.groupby(axis=1, by=by)

    modin_groupby_equals_pandas(modin_groupby, pandas_groupby)
    eval_ngroups(modin_groupby, pandas_groupby)
    eval_shift(modin_groupby, pandas_groupby)
    eval_skew(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.ffill())
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.sem(),
        modin_df_almost_equals_pandas,
    )
    eval_mean(modin_groupby, pandas_groupby)
    eval_any(modin_groupby, pandas_groupby)
    eval_min(modin_groupby, pandas_groupby)
    eval_ndim(modin_groupby, pandas_groupby)

    eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmax())
    eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmin())
    eval_quantile(modin_groupby, pandas_groupby)

    # https://github.com/pandas-dev/pandas/issues/21127
    # eval_cumsum(modin_groupby, pandas_groupby)
    # eval_cummax(modin_groupby, pandas_groupby)
    # eval_cummin(modin_groupby, pandas_groupby)
    # eval_cumprod(modin_groupby, pandas_groupby)

    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.pct_change(),
        modin_df_almost_equals_pandas,
    )
    apply_functions = [lambda df: -df, lambda df: df.sum(axis=1)]
    for func in apply_functions:
        eval_apply(modin_groupby, pandas_groupby, func)

    eval_general(modin_groupby, pandas_groupby, lambda df: df.first())
    eval_general(modin_groupby, pandas_groupby, lambda df: df.backfill())
    eval_general(modin_groupby, pandas_groupby, lambda df: df.bfill())
    eval_prod(modin_groupby, pandas_groupby)
    eval_std(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.last())
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.mad(),
        modin_df_almost_equals_pandas,
    )
    eval_max(modin_groupby, pandas_groupby)
    eval_var(modin_groupby, pandas_groupby)
    eval_len(modin_groupby, pandas_groupby)
    eval_sum(modin_groupby, pandas_groupby)

    # Pandas fails on this case with ValueError
    # eval_ngroup(modin_groupby, pandas_groupby)
    # eval_nunique(modin_groupby, pandas_groupby)
    # NotImplementedError: DataFrameGroupBy.value_counts only handles axis=0
    # eval_value_counts(modin_groupby, pandas_groupby)
    eval_median(modin_groupby, pandas_groupby)
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.cov(),
        modin_df_almost_equals_pandas,
    )

    transform_functions = [lambda df: df + 4, lambda df: -df - 10]
    for func in transform_functions:
        eval_transform(modin_groupby, pandas_groupby, func)

    pipe_functions = [lambda dfgb: dfgb.sum()]
    for func in pipe_functions:
        eval_pipe(modin_groupby, pandas_groupby, func)

    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda df: df.corr(),
        modin_df_almost_equals_pandas,
    )
    eval_fillna(modin_groupby, pandas_groupby)
    eval_count(modin_groupby, pandas_groupby)
    eval_size(modin_groupby, pandas_groupby)
    eval_general(modin_groupby, pandas_groupby, lambda df: df.take([0]))
    eval_groups(modin_groupby, pandas_groupby)


@pytest.mark.parametrize(
    "by", [np.random.randint(0, 100, size=2**8), lambda x: x % 3, None]
)
@pytest.mark.parametrize("as_index_series_or_dataframe", [0, 1, 2])
def test_series_groupby(by, as_index_series_or_dataframe):
    if as_index_series_or_dataframe <= 1:
        as_index = as_index_series_or_dataframe == 1
        series_data = np.random.randint(97, 198, size=2**8)
        modin_series = pd.Series(series_data)
        pandas_series = pandas.Series(series_data)
    else:
        as_index = True
        pandas_series = pandas.DataFrame(
            {
                "col1": [0, 1, 2, 3],
                "col2": [4, 5, 6, 7],
                "col3": [3, 8, 12, 10],
                "col4": [17, 13, 16, 15],
                "col5": [-4, -5, -6, -7],
            }
        )
        modin_series = from_pandas(pandas_series)
        if isinstance(by, np.ndarray) or by is None:
            by = np.random.randint(0, 100, size=len(pandas_series.index))

    n = 1

    try:
        pandas_groupby = pandas_series.groupby(by, as_index=as_index)
        if as_index_series_or_dataframe == 2:
            pandas_groupby = pandas_groupby["col1"]
    except Exception as err:
        with pytest.raises(type(err)):
            modin_series.groupby(by, as_index=as_index)
    else:
        modin_groupby = modin_series.groupby(by, as_index=as_index)
        if as_index_series_or_dataframe == 2:
            modin_groupby = modin_groupby["col1"]

        modin_groupby_equals_pandas(modin_groupby, pandas_groupby)
        eval_ngroups(modin_groupby, pandas_groupby)
        eval_shift(modin_groupby, pandas_groupby)
        eval_general(modin_groupby, pandas_groupby, lambda df: df.ffill())
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.sem(),
            modin_df_almost_equals_pandas,
        )
        eval_general(
            modin_groupby, pandas_groupby, lambda df: df.sample(random_state=1)
        )
        eval_general(modin_groupby, pandas_groupby, lambda df: df.ewm(com=0.5).std())
        eval_general(
            modin_groupby, pandas_groupby, lambda df: df.is_monotonic_decreasing
        )
        eval_general(
            modin_groupby, pandas_groupby, lambda df: df.is_monotonic_increasing
        )
        eval_general(modin_groupby, pandas_groupby, lambda df: df.nlargest())
        eval_general(modin_groupby, pandas_groupby, lambda df: df.nsmallest())
        eval_general(modin_groupby, pandas_groupby, lambda df: df.unique())
        eval_general(modin_groupby, pandas_groupby, lambda df: df.dtype)
        eval_mean(modin_groupby, pandas_groupby)
        eval_any(modin_groupby, pandas_groupby)
        eval_min(modin_groupby, pandas_groupby)
        eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmax())
        eval_ndim(modin_groupby, pandas_groupby)
        eval_cumsum(modin_groupby, pandas_groupby)
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.pct_change(),
            modin_df_almost_equals_pandas,
        )
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.diff(periods=2),
        )
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.diff(periods=-1),
        )
        eval_cummax(modin_groupby, pandas_groupby)

        apply_functions = [lambda df: df.sum(), min]
        for func in apply_functions:
            eval_apply(modin_groupby, pandas_groupby, func)

        eval_general(modin_groupby, pandas_groupby, lambda df: df.first())
        eval_general(modin_groupby, pandas_groupby, lambda df: df.backfill())
        eval_cummin(modin_groupby, pandas_groupby)
        eval_general(modin_groupby, pandas_groupby, lambda df: df.bfill())
        eval_general(modin_groupby, pandas_groupby, lambda df: df.idxmin())
        eval_prod(modin_groupby, pandas_groupby)
        if as_index:
            eval_std(modin_groupby, pandas_groupby)
            eval_var(modin_groupby, pandas_groupby)
            eval_skew(modin_groupby, pandas_groupby)

        agg_functions = [
            lambda df: df.sum(),
            "min",
            "max",
            max,
            sum,
            np.mean,
            ["min", "max"],
            [np.mean, np.std, np.var, np.max, np.min],
        ]
        for func in agg_functions:
            eval_agg(modin_groupby, pandas_groupby, func)
            eval_aggregate(modin_groupby, pandas_groupby, func)

        eval_general(modin_groupby, pandas_groupby, lambda df: df.last())
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda df: df.mad(),
            modin_df_almost_equals_pandas,
        )
        eval_rank(modin_groupby, pandas_groupby)
        eval_max(modin_groupby, pandas_groupby)
        eval_len(modin_groupby, pandas_groupby)
        eval_sum(modin_groupby, pandas_groupby)
        eval_size(modin_groupby, pandas_groupby)
        eval_ngroup(modin_groupby, pandas_groupby)
        eval_nunique(modin_groupby, pandas_groupby)
        eval_value_counts(modin_groupby, pandas_groupby)
        eval_median(modin_groupby, pandas_groupby)
        eval_general(modin_groupby, pandas_groupby, lambda df: df.head(n))
        eval_cumprod(modin_groupby, pandas_groupby)

        transform_functions = [lambda df: df + 4, lambda df: -df - 10]
        for func in transform_functions:
            eval_transform(modin_groupby, pandas_groupby, func)

        pipe_functions = [lambda dfgb: dfgb.sum()]
        for func in pipe_functions:
            eval_pipe(modin_groupby, pandas_groupby, func)

        eval_fillna(modin_groupby, pandas_groupby)
        eval_count(modin_groupby, pandas_groupby)
        eval_general(modin_groupby, pandas_groupby, lambda df: df.tail(n))
        eval_quantile(modin_groupby, pandas_groupby)
        eval_general(modin_groupby, pandas_groupby, lambda df: df.take([0]))
        eval_groups(modin_groupby, pandas_groupby)


def test_multi_column_groupby():
    pandas_df = pandas.DataFrame(
        {
            "col1": np.random.randint(0, 100, size=1000),
            "col2": np.random.randint(0, 100, size=1000),
            "col3": np.random.randint(0, 100, size=1000),
            "col4": np.random.randint(0, 100, size=1000),
            "col5": np.random.randint(0, 100, size=1000),
        },
        index=["row{}".format(i) for i in range(1000)],
    )

    modin_df = from_pandas(pandas_df)
    by = ["col1", "col2"]

    df_equals(modin_df.groupby(by).count(), pandas_df.groupby(by).count())

    with pytest.warns(UserWarning):
        for k, _ in modin_df.groupby(by):
            assert isinstance(k, tuple)

    by = ["row0", "row1"]
    with pytest.raises(KeyError):
        modin_df.groupby(by, axis=1).count()


def sort_index_if_experimental_groupby(*dfs):
    """
    This method should be applied before comparing results of ``groupby.transform`` as
    the experimental implementation changes the order of rows for that:
    https://github.com/modin-project/modin/issues/5924
    """
    if ExperimentalGroupbyImpl.get():
        return tuple(df.sort_index() for df in dfs)
    return dfs


def eval_ngroups(modin_groupby, pandas_groupby):
    assert modin_groupby.ngroups == pandas_groupby.ngroups


def eval_skew(modin_groupby, pandas_groupby):
    modin_df_almost_equals_pandas(modin_groupby.skew(), pandas_groupby.skew())


def eval_mean(modin_groupby, pandas_groupby):
    modin_df_almost_equals_pandas(modin_groupby.mean(), pandas_groupby.mean())


def eval_any(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.any(), pandas_groupby.any())


def eval_min(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.min(), pandas_groupby.min())


def eval_ndim(modin_groupby, pandas_groupby):
    assert modin_groupby.ndim == pandas_groupby.ndim


def eval_cumsum(modin_groupby, pandas_groupby, axis=0):
    df_equals(
        *sort_index_if_experimental_groupby(
            modin_groupby.cumsum(axis=axis), pandas_groupby.cumsum(axis=axis)
        )
    )


def eval_cummax(modin_groupby, pandas_groupby, axis=0):
    df_equals(
        *sort_index_if_experimental_groupby(
            modin_groupby.cummax(axis=axis), pandas_groupby.cummax(axis=axis)
        )
    )


def eval_apply(modin_groupby, pandas_groupby, func):
    df_equals(modin_groupby.apply(func), pandas_groupby.apply(func))


def eval_dtypes(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.dtypes, pandas_groupby.dtypes)


def eval_cummin(modin_groupby, pandas_groupby, axis=0):
    df_equals(
        *sort_index_if_experimental_groupby(
            modin_groupby.cummin(axis=axis), pandas_groupby.cummin(axis=axis)
        )
    )


def eval_prod(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.prod(), pandas_groupby.prod())


def eval_std(modin_groupby, pandas_groupby):
    modin_df_almost_equals_pandas(modin_groupby.std(), pandas_groupby.std())


def eval_aggregate(modin_groupby, pandas_groupby, func):
    df_equals(modin_groupby.aggregate(func), pandas_groupby.aggregate(func))


def eval_agg(modin_groupby, pandas_groupby, func):
    df_equals(modin_groupby.agg(func), pandas_groupby.agg(func))


def eval_rank(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.rank(), pandas_groupby.rank())


def eval_max(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.max(), pandas_groupby.max())


def eval_var(modin_groupby, pandas_groupby):
    modin_df_almost_equals_pandas(modin_groupby.var(), pandas_groupby.var())


def eval_len(modin_groupby, pandas_groupby):
    assert len(modin_groupby) == len(pandas_groupby)


def eval_sum(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.sum(), pandas_groupby.sum())


def eval_ngroup(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.ngroup(), pandas_groupby.ngroup())


def eval_nunique(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.nunique(), pandas_groupby.nunique())


def eval_value_counts(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.value_counts(), pandas_groupby.value_counts())


def eval_median(modin_groupby, pandas_groupby):
    modin_df_almost_equals_pandas(modin_groupby.median(), pandas_groupby.median())


def eval_cumprod(modin_groupby, pandas_groupby, axis=0):
    df_equals(
        *sort_index_if_experimental_groupby(
            modin_groupby.cumprod(), pandas_groupby.cumprod()
        )
    )
    df_equals(
        *sort_index_if_experimental_groupby(
            modin_groupby.cumprod(axis=axis), pandas_groupby.cumprod(axis=axis)
        )
    )


def eval_transform(modin_groupby, pandas_groupby, func):
    df_equals(
        *sort_index_if_experimental_groupby(
            modin_groupby.transform(func), pandas_groupby.transform(func)
        )
    )


def eval_fillna(modin_groupby, pandas_groupby):
    df_equals(
        *sort_index_if_experimental_groupby(
            modin_groupby.fillna(method="ffill"), pandas_groupby.fillna(method="ffill")
        )
    )


def eval_count(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.count(), pandas_groupby.count())


def eval_size(modin_groupby, pandas_groupby):
    df_equals(modin_groupby.size(), pandas_groupby.size())


def eval_pipe(modin_groupby, pandas_groupby, func):
    df_equals(modin_groupby.pipe(func), pandas_groupby.pipe(func))


def eval_quantile(modin_groupby, pandas_groupby):
    try:
        pandas_result = pandas_groupby.quantile(q=0.4)
    except Exception as err:
        with pytest.raises(type(err)):
            modin_groupby.quantile(q=0.4)
    else:
        df_equals(modin_groupby.quantile(q=0.4), pandas_result)


def eval___getattr__(modin_groupby, pandas_groupby, item):
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda grp: grp[item].count(),
        comparator=build_types_asserter(df_equals),
    )
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda grp: getattr(grp, item).count(),
        comparator=build_types_asserter(df_equals),
    )


def eval___getitem__(md_grp, pd_grp, item):
    eval_general(
        md_grp,
        pd_grp,
        lambda grp: grp[item].mean(),
        comparator=build_types_asserter(df_equals),
    )
    eval_general(
        md_grp,
        pd_grp,
        lambda grp: grp[item].count(),
        comparator=build_types_asserter(df_equals),
    )

    def build_list_agg(fns):
        def test(grp):
            res = grp[item].agg(fns)
            if res.ndim == 2:
                # Modin's frame has an extra level in the result. Alligning columns to compare.
                # https://github.com/modin-project/modin/issues/3490
                res = res.set_axis(fns, axis=1)
            return res

        return test

    # issue-#3252
    eval_general(
        md_grp,
        pd_grp,
        build_list_agg(["mean"]),
        comparator=build_types_asserter(df_equals),
    )
    eval_general(
        md_grp,
        pd_grp,
        build_list_agg(["mean", "count"]),
        comparator=build_types_asserter(df_equals),
    )
    # Explicit default-to-pandas test
    eval_general(
        md_grp,
        pd_grp,
        # Defaulting to pandas only for Modin groupby objects
        lambda grp: grp[item].sum()
        if not isinstance(grp, pd.groupby.DataFrameGroupBy)
        else grp[item]._default_to_pandas(lambda df: df.sum()),
        comparator=build_types_asserter(df_equals),
    )


def eval_groups(modin_groupby, pandas_groupby):
    for k, v in modin_groupby.groups.items():
        assert v.equals(pandas_groupby.groups[k])
    if ExperimentalGroupbyImpl.get():
        # `.get_group()` doesn't work correctly with experimental groupby:
        # https://github.com/modin-project/modin/issues/6093
        return
    for name in pandas_groupby.groups:
        df_equals(modin_groupby.get_group(name), pandas_groupby.get_group(name))


def eval_shift(modin_groupby, pandas_groupby):
    def comparator(df1, df2):
        df_equals(*sort_index_if_experimental_groupby(df1, df2))

    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda groupby: groupby.shift(),
        comparator=comparator,
    )
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda groupby: groupby.shift(periods=0),
        comparator=comparator,
    )
    eval_general(
        modin_groupby,
        pandas_groupby,
        lambda groupby: groupby.shift(periods=-3),
        comparator=comparator,
    )

    # Disabled for `BaseOnPython` because of the issue with `getitem_array`.
    # groupby.shift internally masks the source frame with a Series boolean mask,
    # doing so ends up in the `getitem_array` method, that is broken for `BaseOnPython`:
    # https://github.com/modin-project/modin/issues/3701
    if get_current_execution() != "BaseOnPython":
        if isinstance(pandas_groupby, pandas.core.groupby.DataFrameGroupBy):
            pandas_res = pandas_groupby.shift(axis=1, fill_value=777)
            modin_res = modin_groupby.shift(axis=1, fill_value=777)
            # Pandas produces unexpected index order (pandas GH 44269).
            # Here we align index of Modin result with pandas to make test passed.
            import pandas.core.algorithms as algorithms

            indexer, _ = modin_res.index.get_indexer_non_unique(modin_res.index._values)
            indexer = algorithms.unique1d(indexer)
            modin_res = modin_res.take(indexer)

            comparator(modin_res, pandas_res)
        else:
            eval_general(
                modin_groupby,
                pandas_groupby,
                lambda groupby: groupby.shift(axis=1, fill_value=777),
                comparator=comparator,
            )


def test_groupby_on_index_values_with_loop():
    length = 2**6
    data = {
        "a": np.random.randint(0, 100, size=length),
        "b": np.random.randint(0, 100, size=length),
        "c": np.random.randint(0, 100, size=length),
    }
    idx = ["g1" if i % 3 != 0 else "g2" for i in range(length)]
    modin_df = pd.DataFrame(data, index=idx, columns=list("aba"))
    pandas_df = pandas.DataFrame(data, index=idx, columns=list("aba"))
    modin_groupby_obj = modin_df.groupby(modin_df.index)
    pandas_groupby_obj = pandas_df.groupby(pandas_df.index)

    modin_dict = {k: v for k, v in modin_groupby_obj}
    pandas_dict = {k: v for k, v in pandas_groupby_obj}

    for k in modin_dict:
        df_equals(modin_dict[k], pandas_dict[k])

    modin_groupby_obj = modin_df.groupby(modin_df.columns, axis=1)
    pandas_groupby_obj = pandas_df.groupby(pandas_df.columns, axis=1)

    modin_dict = {k: v for k, v in modin_groupby_obj}
    pandas_dict = {k: v for k, v in pandas_groupby_obj}

    for k in modin_dict:
        df_equals(modin_dict[k], pandas_dict[k])


def test_groupby_getitem_preserves_key_order_issue_6154():
    a = np.tile(["a", "b", "c", "d", "e"], (1, 10))
    np.random.shuffle(a[0])
    df = pd.DataFrame(
        np.hstack((a.T, np.arange(100).reshape((50, 2)))),
        columns=["col 1", "col 2", "col 3"],
    )
    eval_general(
        df, df._to_pandas(), lambda df: df.groupby("col 1")[["col 3", "col 2"]].count()
    )


@pytest.mark.parametrize(
    "groupby_kwargs",
    [
        pytest.param({"level": 1, "axis": 1}, id="level_idx_axis=1"),
        pytest.param({"level": 1}, id="level_idx"),
        pytest.param({"level": [1, "four"]}, id="level_idx+name"),
        pytest.param({"by": "four"}, id="level_name"),
        pytest.param({"by": ["one", "two"]}, id="level_name_multi_by"),
        pytest.param({"by": ["item0", "one", "two"]}, id="col_name+level_name"),
    ],
)
def test_groupby_multiindex(groupby_kwargs):
    frame_data = np.random.randint(0, 100, size=(2**6, 2**4))
    modin_df = pd.DataFrame(frame_data)
    pandas_df = pandas.DataFrame(frame_data)

    new_index = pandas.Index([f"item{i}" for i in range(len(pandas_df))])
    new_columns = pandas.MultiIndex.from_tuples(
        [(i // 4, i // 2, i) for i in modin_df.columns], names=["four", "two", "one"]
    )
    modin_df.columns = new_columns
    modin_df.index = new_index
    pandas_df.columns = new_columns
    pandas_df.index = new_index

    if groupby_kwargs.get("axis", 0) == 0:
        modin_df = modin_df.T
        pandas_df = pandas_df.T

    md_grp, pd_grp = (
        modin_df.groupby(**groupby_kwargs),
        pandas_df.groupby(**groupby_kwargs),
    )
    modin_groupby_equals_pandas(md_grp, pd_grp)
    df_equals(md_grp.sum(), pd_grp.sum())
    df_equals(md_grp.size(), pd_grp.size())
    # Grouping on level works incorrect in case of aggregation:
    # https://github.com/modin-project/modin/issues/2912
    # df_equals(md_grp.quantile(), pd_grp.quantile())
    df_equals(md_grp.first(), pd_grp.first())


@pytest.mark.parametrize("dropna", [True, False])
@pytest.mark.parametrize(
    "groupby_kwargs",
    [
        pytest.param({"level": 1, "axis": 1}, id="level_idx_axis=1"),
        pytest.param({"level": 1}, id="level_idx"),
        pytest.param({"level": [1, "four"]}, id="level_idx+name"),
        pytest.param({"by": "four"}, id="level_name"),
        pytest.param({"by": ["one", "two"]}, id="level_name_multi_by"),
        pytest.param(
            {"by": ["item0", "one", "two"]},
            id="col_name+level_name",
        ),
        pytest.param(
            {"by": ["item0"]},
            id="col_name",
        ),
        pytest.param(
            {"by": ["item0", "item1"]},
            id="col_name_multi_by",
        ),
    ],
)
def test_groupby_with_kwarg_dropna(groupby_kwargs, dropna):
    modin_df = pd.DataFrame(test_data["float_nan_data"])
    pandas_df = pandas.DataFrame(test_data["float_nan_data"])

    new_index = pandas.Index([f"item{i}" for i in range(len(pandas_df))])
    new_columns = pandas.MultiIndex.from_tuples(
        [(i // 4, i // 2, i) for i in range(len(modin_df.columns))],
        names=["four", "two", "one"],
    )
    modin_df.columns = new_columns
    modin_df.index = new_index
    pandas_df.columns = new_columns
    pandas_df.index = new_index

    if groupby_kwargs.get("axis", 0) == 0:
        modin_df = modin_df.T
        pandas_df = pandas_df.T

    md_grp, pd_grp = (
        modin_df.groupby(**groupby_kwargs, dropna=dropna),
        pandas_df.groupby(**groupby_kwargs, dropna=dropna),
    )
    modin_groupby_equals_pandas(md_grp, pd_grp)

    by_kwarg = groupby_kwargs.get("by", [])
    # Disabled because of broken `dropna=False` for TreeReduce implemented aggs:
    # https://github.com/modin-project/modin/issues/3817
    if not (
        not dropna
        and len(by_kwarg) > 1
        and any(col in modin_df.columns for col in by_kwarg)
    ):
        df_equals(md_grp.sum(), pd_grp.sum())
        df_equals(md_grp.size(), pd_grp.size())
    # Grouping on level works incorrect in case of aggregation:
    # https://github.com/modin-project/modin/issues/2912
    # "BaseOnPython" tests are disabled because of the bug:
    # https://github.com/modin-project/modin/issues/3827
    if get_current_execution() != "BaseOnPython" and any(
        col in modin_df.columns for col in by_kwarg
    ):
        df_equals(md_grp.quantile(), pd_grp.quantile())
    # Default-to-pandas tests are disabled for multi-column 'by' because of the bug:
    # https://github.com/modin-project/modin/issues/3827
    if not (not dropna and len(by_kwarg) > 1):
        df_equals(md_grp.first(), pd_grp.first())
        df_equals(md_grp._default_to_pandas(lambda df: df.sum()), pd_grp.sum())


@pytest.mark.parametrize("groupby_axis", [0, 1])
@pytest.mark.parametrize("shift_axis", [0, 1])
@pytest.mark.parametrize("groupby_sort", [True, False])
def test_shift_freq(groupby_axis, shift_axis, groupby_sort):
    pandas_df = pandas.DataFrame(
        {
            "col1": [1, 0, 2, 3],
            "col2": [4, 5, np.NaN, 7],
            "col3": [np.NaN, np.NaN, 12, 10],
            "col4": [17, 13, 16, 15],
        }
    )
    modin_df = from_pandas(pandas_df)

    new_index = pandas.date_range("1/12/2020", periods=4, freq="S")
    if groupby_axis == 0 and shift_axis == 0:
        pandas_df.index = modin_df.index = new_index
        by = [["col2", "col3"], ["col2"], ["col4"], [0, 1, 0, 2]]
    else:
        pandas_df.index = modin_df.index = new_index
        pandas_df.columns = modin_df.columns = new_index
        by = [[0, 1, 0, 2]]

    for _by in by:
        pandas_groupby = pandas_df.groupby(by=_by, axis=groupby_axis, sort=groupby_sort)
        modin_groupby = modin_df.groupby(by=_by, axis=groupby_axis, sort=groupby_sort)
        eval_general(
            modin_groupby,
            pandas_groupby,
            lambda groupby: groupby.shift(axis=shift_axis, freq="S"),
        )


@pytest.mark.parametrize(
    "by_and_agg_dict",
    [
        {
            "by": [
                list(test_data["int_data"].keys())[0],
                list(test_data["int_data"].keys())[1],
            ],
            "agg_dict": {
                "max": (list(test_data["int_data"].keys())[2], np.max),
                "min": (list(test_data["int_data"].keys())[2], np.min),
            },
        },
        {
            "by": ["col1"],
            "agg_dict": {
                "max": (list(test_data["int_data"].keys())[0], np.max),
                "min": (list(test_data["int_data"].keys())[-1], np.min),
            },
        },
        {
            "by": [
                list(test_data["int_data"].keys())[0],
                list(test_data["int_data"].keys())[-1],
            ],
            "agg_dict": {
                "max": (list(test_data["int_data"].keys())[1], max),
                "min": (list(test_data["int_data"].keys())[-2], min),
            },
        },
        pytest.param(
            {
                "by": [
                    list(test_data["int_data"].keys())[0],
                    list(test_data["int_data"].keys())[-1],
                ],
                "agg_dict": {
                    "max": (list(test_data["int_data"].keys())[1], max),
                    "min": (list(test_data["int_data"].keys())[-1], min),
                },
            },
            marks=pytest.mark.skip("See Modin issue #3602"),
        ),
    ],
)
@pytest.mark.parametrize("as_index", [True, False])
def test_agg_func_None_rename(by_and_agg_dict, as_index):
    modin_df, pandas_df = create_test_dfs(test_data["int_data"])

    modin_result = modin_df.groupby(by_and_agg_dict["by"], as_index=as_index).agg(
        **by_and_agg_dict["agg_dict"]
    )
    pandas_result = pandas_df.groupby(by_and_agg_dict["by"], as_index=as_index).agg(
        **by_and_agg_dict["agg_dict"]
    )
    df_equals(modin_result, pandas_result)


@pytest.mark.parametrize(
    "as_index",
    [
        True,
        pytest.param(
            False,
            marks=pytest.mark.skipif(
                get_current_execution() == "BaseOnPython"
                or ExperimentalGroupbyImpl.get(),
                reason="See Pandas issue #39103",
            ),
        ),
    ],
)
@pytest.mark.parametrize("by_length", [1, 3])
@pytest.mark.parametrize(
    "agg_fns",
    [["sum", "min", "max"], ["mean", "quantile"]],
    ids=["reduce", "aggregation"],
)
@pytest.mark.parametrize(
    "intersection_with_by_cols",
    [pytest.param(True, marks=pytest.mark.skip("See Modin issue #3602")), False],
)
def test_dict_agg_rename_mi_columns(
    as_index, by_length, agg_fns, intersection_with_by_cols
):
    md_df, pd_df = create_test_dfs(test_data["int_data"])
    mi_columns = generate_multiindex(len(md_df.columns), nlevels=4)

    md_df.columns, pd_df.columns = mi_columns, mi_columns

    by = list(md_df.columns[:by_length])
    agg_cols = (
        list(md_df.columns[by_length - 1 : by_length + 2])
        if intersection_with_by_cols
        else list(md_df.columns[by_length : by_length + 3])
    )

    agg_dict = {
        f"custom-{i}" + str(agg_fns[i % len(agg_fns)]): (col, agg_fns[i % len(agg_fns)])
        for i, col in enumerate(agg_cols)
    }

    md_res = md_df.groupby(by, as_index=as_index).agg(**agg_dict)
    pd_res = pd_df.groupby(by, as_index=as_index).agg(**agg_dict)

    df_equals(md_res, pd_res)


def test_agg_4604():
    data = {"col1": [1, 2], "col2": [3, 4]}
    modin_df, pandas_df = pd.DataFrame(data), pandas.DataFrame(data)
    # add another partition
    modin_df["col3"] = modin_df["col1"]
    pandas_df["col3"] = pandas_df["col1"]

    # problem only with custom aggregation function
    def col3(x):
        return np.max(x)

    by = ["col1"]
    agg_func = {"col2": ["sum", "min"], "col3": col3}

    modin_groupby, pandas_groupby = modin_df.groupby(by), pandas_df.groupby(by)
    eval_agg(modin_groupby, pandas_groupby, agg_func)


@pytest.mark.parametrize(
    "operation",
    [
        "quantile",
        pytest.param(
            "mean",
            marks=pytest.mark.xfail(
                condition=ExperimentalGroupbyImpl.get()
                and Engine.get() in ("Dask", "Ray", "Unidist"),
                reason="There's a bug in pandas making this test to fail that's been fixed in 2.0;"
                + "Remove this after the transition to pandas 2.0",
            ),
        ),
        pytest.param(
            "sum", marks=pytest.mark.skip("See Modin issue #2255 for details")
        ),
        pytest.param(
            "median",
            marks=pytest.mark.xfail(
                condition=ExperimentalGroupbyImpl.get()
                and Engine.get() in ("Dask", "Ray", "Unidist"),
                reason="There's a bug in pandas making this test to fail that's been fixed in 2.0;"
                + "Remove this after the transition to pandas 2.0",
            ),
        ),
        "unique",
        "cumprod",
    ],
)
def test_agg_exceptions(operation):
    N = 256
    fill_data = [
        (
            "nan_column",
            [
                np.datetime64("2010"),
                None,
                np.datetime64("2007"),
                np.datetime64("2010"),
                np.datetime64("2006"),
                np.datetime64("2012"),
                None,
                np.datetime64("2011"),
            ]
            * (N // 8),
        ),
        (
            "date_column",
            [
                np.datetime64("2010"),
                np.datetime64("2011"),
                np.datetime64("2011-06-15T00:00"),
                np.datetime64("2009-01-01"),
            ]
            * (N // 4),
        ),
    ]

    data1 = {
        "column_to_by": ["foo", "bar", "baz", "bar"] * (N // 4),
        "nan_column": [None] * N,
    }

    data2 = {
        f"{key}{i}": value
        for key, value in fill_data
        for i in range(N // len(fill_data))
    }

    data = {**data1, **data2}

    def comparator(df1, df2):
        from modin.core.dataframe.algebra.default2pandas.groupby import GroupBy

        if GroupBy.is_transformation_kernel(operation):
            df1, df2 = sort_index_if_experimental_groupby(df1, df2)

        df_equals(df1, df2)

    eval_aggregation(*create_test_dfs(data), operation=operation, comparator=comparator)


@pytest.mark.skip(
    "Pandas raises a ValueError on empty dictionary aggregation since 1.2.0"
    + "It's unclear is that was made on purpose or it is a bug. That question"
    + "was asked in https://github.com/pandas-dev/pandas/issues/39609."
    + "So until the answer this test is disabled."
)
@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "Max": ("cnt", np.max),
            "Sum": ("cnt", np.sum),
            "Num": ("c", pd.Series.nunique),
            "Num1": ("c", pandas.Series.nunique),
        },
        {
            "func": {
                "Max": ("cnt", np.max),
                "Sum": ("cnt", np.sum),
                "Num": ("c", pd.Series.nunique),
                "Num1": ("c", pandas.Series.nunique),
            }
        },
    ],
)
def test_to_pandas_convertion(kwargs):
    data = {"a": [1, 2], "b": [3, 4], "c": [5, 6]}
    by = ["a", "b"]

    eval_aggregation(*create_test_dfs(data), by=by, **kwargs)


@pytest.mark.parametrize(
    # When True, do df[name], otherwise just use name
    "columns",
    [
        [(False, "a"), (False, "b"), (False, "c")],
        [(False, "a"), (False, "b")],
        [(True, "a"), (True, "b"), (True, "c")],
        [(True, "a"), (True, "b")],
        [(False, "a"), (False, "b"), (True, "c")],
        [(False, "a"), (True, "c")],
    ],
)
def test_mixed_columns(columns):
    def get_columns(df):
        return [df[name] if lookup else name for (lookup, name) in columns]

    data = {"a": [1, 1, 2], "b": [11, 11, 22], "c": [111, 111, 222]}

    df1 = pandas.DataFrame(data)
    df1 = pandas.concat([df1])
    ref = df1.groupby(get_columns(df1)).size()

    df2 = pd.DataFrame(data)
    df2 = pd.concat([df2])
    exp = df2.groupby(get_columns(df2)).size()
    df_equals(ref, exp)


@pytest.mark.parametrize(
    # When True, use (df[name] + 1), otherwise just use name
    "columns",
    [
        [(True, "a"), (True, "b"), (True, "c")],
        [(True, "a"), (True, "b")],
        [(False, "a"), (False, "b"), (True, "c")],
        [(False, "a"), (True, "c")],
        [(False, "a"), (True, "c"), (False, [1, 1, 2])],
        [(False, "a"), (False, "b"), (False, "c")],
        [(False, "a"), (False, "b"), (False, "c"), (False, [1, 1, 2])],
    ],
)
def test_internal_by_detection(columns):
    def get_columns(df):
        return [(df[name] + 1) if lookup else name for (lookup, name) in columns]

    data = {"a": [1, 1, 2], "b": [11, 11, 22], "c": [111, 111, 222]}

    md_df = pd.DataFrame(data)
    by = get_columns(md_df)
    md_grp = md_df.groupby(by)

    ref = frozenset(
        col for is_lookup, col in columns if not is_lookup and hashable(col)
    )
    exp = frozenset(md_grp._internal_by)

    assert ref == exp


@pytest.mark.parametrize(
    # When True, use (df[name] + 1), otherwise just use name
    "columns",
    [
        [(True, "a"), (True, "b"), (True, "c")],
        [(True, "a"), (True, "b")],
        [(False, "a"), (False, "b"), (True, "c")],
        [(False, "a"), (True, "c")],
        [(False, "a"), (True, "c"), (False, [1, 1, 2])],
    ],
)
@pytest.mark.parametrize("as_index", [True, False])
def test_mixed_columns_not_from_df(columns, as_index):
    """
    Unlike the previous test, in this case the Series is not just a column from
    the original DataFrame, so you can't use a fasttrack.
    """

    def get_columns(df):
        return [(df[name] + 1) if lookup else name for (lookup, name) in columns]

    data = {"a": [1, 1, 2], "b": [11, 11, 22], "c": [111, 111, 222]}
    groupby_kw = {"as_index": as_index}

    md_df, pd_df = create_test_dfs(data)
    by_md, by_pd = map(get_columns, [md_df, pd_df])

    pd_grp = pd_df.groupby(by_pd, **groupby_kw)
    md_grp = md_df.groupby(by_md, **groupby_kw)

    modin_groupby_equals_pandas(md_grp, pd_grp)
    eval_general(md_grp, pd_grp, lambda grp: grp.size())
    eval_general(md_grp, pd_grp, lambda grp: grp.apply(lambda df: df.sum()))
    eval_general(md_grp, pd_grp, lambda grp: grp.first())


@pytest.mark.parametrize(
    # When True, do df[obj], otherwise just use the obj
    "columns",
    [
        [(False, "a")],
        [(False, "a"), (False, "b"), (False, "c")],
        [(False, "a"), (False, "b")],
        [(False, "b"), (False, "a")],
        [(True, "a"), (True, "b"), (True, "c")],
        [(True, "a"), (True, "b")],
        [(False, "a"), (False, "b"), (True, "c")],
        [(False, "a"), (True, "c")],
        [(False, "a"), (False, pd.Series([5, 6, 7, 8]))],
    ],
)
def test_unknown_groupby(columns):
    def get_columns(df):
        return [df[name] if lookup else name for (lookup, name) in columns]

    data = {"b": [11, 11, 22, 200], "c": [111, 111, 222, 7000]}
    modin_df, pandas_df = pd.DataFrame(data), pandas.DataFrame(data)

    with pytest.raises(KeyError):
        pandas_df.groupby(by=get_columns(pandas_df))
    with pytest.raises(KeyError):
        modin_df.groupby(by=get_columns(modin_df))


@pytest.mark.parametrize(
    "func_to_apply",
    [
        lambda df: df.sum(),
        lambda df: df.size(),
        lambda df: df.quantile(),
        lambda df: df.dtypes,
        lambda df: df.apply(lambda df: df.sum()),
        pytest.param(
            lambda df: df.apply(lambda df: pandas.Series([1, 2, 3, 4])),
            marks=pytest.mark.skip("See modin issue #2511"),
        ),
        lambda grp: grp.agg(
            {
                list(test_data_values[0].keys())[1]: (max, min, sum),
                list(test_data_values[0].keys())[-2]: (sum, min, max),
            }
        ),
        lambda grp: grp.agg(
            {
                list(test_data_values[0].keys())[1]: [
                    ("new_sum", "sum"),
                    ("new_min", "min"),
                ],
                list(test_data_values[0].keys())[-2]: np.sum,
            }
        ),
        pytest.param(
            lambda grp: grp.agg(
                {
                    list(test_data_values[0].keys())[1]: [
                        ("new_sum", "sum"),
                        ("new_mean", "mean"),
                    ],
                    list(test_data_values[0].keys())[-2]: "skew",
                }
            ),
            id="renaming_aggs_at_different_partitions",
        ),
        pytest.param(
            lambda grp: grp.agg(
                {
                    list(test_data_values[0].keys())[1]: [
                        ("new_sum", "sum"),
                        ("new_mean", "mean"),
                    ],
                    list(test_data_values[0].keys())[2]: "skew",
                }
            ),
            id="renaming_aggs_at_same_partition",
        ),
        pytest.param(
            lambda grp: grp.agg(
                {
                    list(test_data_values[0].keys())[1]: "mean",
                    list(test_data_values[0].keys())[-2]: "skew",
                }
            ),
            id="custom_aggs_at_different_partitions",
        ),
        pytest.param(
            lambda grp: grp.agg(
                {
                    list(test_data_values[0].keys())[1]: "mean",
                    list(test_data_values[0].keys())[2]: "skew",
                }
            ),
            id="custom_aggs_at_same_partition",
        ),
        pytest.param(
            lambda grp: grp.agg(
                {
                    list(test_data_values[0].keys())[1]: "mean",
                    list(test_data_values[0].keys())[-2]: "sum",
                }
            ),
            id="native_and_custom_aggs_at_different_partitions",
        ),
        pytest.param(
            lambda grp: grp.agg(
                {
                    list(test_data_values[0].keys())[1]: "mean",
                    list(test_data_values[0].keys())[2]: "sum",
                }
            ),
            id="native_and_custom_aggs_at_same_partition",
        ),
        pytest.param(
            lambda grp: grp.agg(
                {
                    list(test_data_values[0].keys())[1]: (max, "mean", sum),
                    list(test_data_values[0].keys())[-1]: (sum, "skew", max),
                }
            ),
            id="Agg_and_by_intersection_TreeReduce_implementation",
        ),
        pytest.param(
            lambda grp: grp.agg(
                {
                    list(test_data_values[0].keys())[1]: (max, "mean", "nunique"),
                    list(test_data_values[0].keys())[-1]: (sum, min, max),
                }
            ),
            id="Agg_and_by_intersection_FullAxis_implementation",
        ),
        pytest.param(
            lambda grp: grp.agg({list(test_data_values[0].keys())[0]: "count"}),
            id="Agg_and_by_intersection_issue_3376",
        ),
    ],
)
@pytest.mark.parametrize("as_index", [True, False])
@pytest.mark.parametrize("by_length", [1, 2])
@pytest.mark.parametrize(
    "categorical_by",
    [pytest.param(True, marks=pytest.mark.skip("See modin issue #2513")), False],
)
def test_multi_column_groupby_different_partitions(
    func_to_apply, as_index, by_length, categorical_by
):
    data = test_data_values[0]
    md_df, pd_df = create_test_dfs(data)

    by = [pd_df.columns[-i if i % 2 else i] for i in range(by_length)]

    if categorical_by:
        md_df = md_df.astype({by[0]: "category"})
        pd_df = pd_df.astype({by[0]: "category"})

    md_grp, pd_grp = (
        md_df.groupby(by, as_index=as_index),
        pd_df.groupby(by, as_index=as_index),
    )
    eval_general(
        md_grp,
        pd_grp,
        func_to_apply,
        # 'skew' and 'mean' results are not 100% equal to pandas as they use
        # different formulas and so precision errors come into play. Thus
        # using a custom comparator that allows slight numeric deviations.
        comparator=try_modin_df_almost_equals_compare,
    )
    eval___getitem__(md_grp, pd_grp, md_df.columns[1])
    eval___getitem__(md_grp, pd_grp, [md_df.columns[1], md_df.columns[2]])


@pytest.mark.parametrize(
    "by",
    [
        0,
        1.5,
        "str",
        pandas.Timestamp("2020-02-02"),
        [0, "str"],
        [pandas.Timestamp("2020-02-02"), 1.5],
    ],
)
@pytest.mark.parametrize("as_index", [True, False])
def test_not_str_by(by, as_index):
    columns = pandas.Index([0, 1.5, "str", pandas.Timestamp("2020-02-02")])
    data = {col: np.arange(5) for col in columns}
    md_df, pd_df = create_test_dfs(data)

    md_grp, pd_grp = (
        md_df.groupby(by, as_index=as_index),
        pd_df.groupby(by, as_index=as_index),
    )

    modin_groupby_equals_pandas(md_grp, pd_grp)
    eval_general(md_grp, pd_grp, lambda grp: grp.sum())
    eval_general(md_grp, pd_grp, lambda grp: grp.size())
    eval_general(md_grp, pd_grp, lambda grp: grp.agg(lambda df: df.mean()))
    eval_general(md_grp, pd_grp, lambda grp: grp.dtypes)
    eval_general(md_grp, pd_grp, lambda grp: grp.first())


@pytest.mark.parametrize("internal_by_length", [0, 1, 2])
@pytest.mark.parametrize("external_by_length", [0, 1, 2])
@pytest.mark.parametrize("has_categorical_by", [True, False])
@pytest.mark.parametrize(
    "agg_func",
    [
        pytest.param(
            lambda grp: grp.apply(lambda df: df.dtypes), id="modin_dtypes_impl"
        ),
        pytest.param(lambda grp: grp.apply(lambda df: df.sum()), id="apply_sum"),
        pytest.param(lambda grp: grp.count(), id="count"),
        pytest.param(lambda grp: grp.nunique(), id="nunique"),
        # Integer key means the index of the column to replace it with.
        # 0 and -1 are considered to be the indices of the columns to group on.
        pytest.param({1: "sum", 2: "nunique"}, id="dict_agg_no_intersection_with_by"),
        pytest.param(
            {0: "mean", 1: "sum", 2: "nunique"},
            id="dict_agg_has_intersection_with_by",
        ),
        pytest.param(
            {1: "sum", 2: "nunique", -1: "nunique"},
            id="dict_agg_has_intersection_with_categorical_by",
        ),
    ],
)
# There are two versions of the `handle_as_index` method: the one accepting pandas.DataFrame from
# the execution kernel and backend agnostic. This parameter indicates which one implementation to use.
@pytest.mark.parametrize("use_backend_agnostic_method", [True, False])
def test_handle_as_index(
    internal_by_length,
    external_by_length,
    has_categorical_by,
    agg_func,
    use_backend_agnostic_method,
    request,
):
    """
    Test ``modin.core.dataframe.algebra.default2pandas.groupby.GroupBy.handle_as_index``.

    The role of the ``handle_as_index`` method is to build a groupby result considering
    ``as_index=False`` from the result that was computed with ``as_index=True``.

    So the testing flow is the following:
        1. Compute GroupBy result with the ``as_index=True`` parameter via Modin.
        2. Build ``as_index=False`` result from the ``as_index=True`` using ``handle_as_index`` method.
        3. Compute GroupBy result with the ``as_index=False`` parameter via pandas as the reference result.
        4. Compare the result from the second step with the reference.
    """
    by_length = internal_by_length + external_by_length
    if by_length == 0:
        pytest.skip("No keys to group on were passed, skipping the test.")

    if (
        has_categorical_by
        and by_length > 1
        and (
            isinstance(agg_func, dict)
            or ("nunique" in request.node.callspec.id.split("-"))
        )
    ):
        pytest.skip(
            "The linked bug makes pandas raise an exception when 'by' is categorical: "
            + "https://github.com/pandas-dev/pandas/issues/36698"
        )

    df = pandas.DataFrame(test_groupby_data)
    external_by_cols = GroupBy.validate_by(df.add_prefix("external_"))

    if has_categorical_by:
        df = df.astype({df.columns[-1]: "category"})

    if isinstance(agg_func, dict):
        agg_func = {df.columns[key]: value for key, value in agg_func.items()}
        selection = list(agg_func.keys())
        agg_dict = agg_func
        agg_func = lambda grp: grp.agg(agg_dict)  # noqa: E731 (lambda assignment)
    else:
        selection = None

    # Selecting 'by' columns from both sides of the frame so they located in different partitions
    internal_by = df.columns[
        range(-internal_by_length // 2, internal_by_length // 2)
    ].tolist()
    external_by = external_by_cols[:external_by_length]

    pd_by = internal_by + external_by
    md_by = internal_by + [pd.Series(ser) for ser in external_by]

    grp_result = pd.DataFrame(df).groupby(md_by, as_index=True)
    grp_reference = df.groupby(pd_by, as_index=False)

    agg_result = agg_func(grp_result)
    agg_reference = agg_func(grp_reference)

    if use_backend_agnostic_method:
        reset_index, drop, lvls_to_drop, cols_to_drop = GroupBy.handle_as_index(
            result_cols=agg_result.columns,
            result_index_names=agg_result.index.names,
            internal_by_cols=internal_by,
            by_cols_dtypes=df[internal_by].dtypes.values,
            by_length=len(md_by),
            selection=selection,
            drop=len(internal_by) != 0,
        )

        if len(lvls_to_drop) > 0:
            agg_result.index = agg_result.index.droplevel(lvls_to_drop)
        if len(cols_to_drop) > 0:
            agg_result = agg_result.drop(columns=cols_to_drop)
        if reset_index:
            agg_result = agg_result.reset_index(drop=drop)
    else:
        GroupBy.handle_as_index_for_dataframe(
            result=agg_result,
            internal_by_cols=internal_by,
            by_cols_dtypes=df[internal_by].dtypes.values,
            by_length=len(md_by),
            selection=selection,
            drop=len(internal_by) != 0,
            inplace=True,
        )

    df_equals(agg_result, agg_reference)


def test_validate_by():
    """Test ``modin.core.dataframe.algebra.default2pandas.groupby.GroupBy.validate_by``."""

    def compare(obj1, obj2):
        assert type(obj1) == type(
            obj2
        ), f"Both objects must be instances of the same type: {type(obj1)} != {type(obj2)}."
        if isinstance(obj1, list):
            for val1, val2 in itertools.zip_longest(obj1, obj2):
                df_equals(val1, val2)
        else:
            df_equals(obj1, obj2)

    # This emulates situation when the Series's query compiler being passed as a 'by':
    #   1. The Series at the QC level is represented as a single-column frame with the `MODIN_UNNAMED_SERIES_LABEL` columns.
    #   2. The valid representation of such QC is an unnamed Series.
    reduced_frame = pandas.DataFrame({MODIN_UNNAMED_SERIES_LABEL: [1, 2, 3]})
    series_result = GroupBy.validate_by(reduced_frame)
    series_reference = [pandas.Series([1, 2, 3], name=None)]
    compare(series_reference, series_result)

    # This emulates situation when several 'by' columns of the group frame are passed as a single QueryCompiler:
    #   1. If grouping on several columns the 'by' at the QC level is the following: ``df[by]._query_compiler``.
    #   2. The valid representation of such QC is a list of Series.
    splited_df = [pandas.Series([1, 2, 3], name=f"col{i}") for i in range(3)]
    splited_df_result = GroupBy.validate_by(
        pandas.concat(splited_df, axis=1, copy=True)
    )
    compare(splited_df, splited_df_result)

    # This emulates situation of mixed by (two column names and an external Series):
    by = ["col1", "col2", pandas.DataFrame({MODIN_UNNAMED_SERIES_LABEL: [1, 2, 3]})]
    result_by = GroupBy.validate_by(by)
    reference_by = ["col1", "col2", pandas.Series([1, 2, 3], name=None)]
    compare(reference_by, result_by)


def test_groupby_with_virtual_partitions():
    # from https://github.com/modin-project/modin/issues/4464
    modin_df, pandas_df = create_test_dfs(test_data["int_data"])

    # Concatenate DataFrames here to make virtual partitions.
    big_modin_df = pd.concat([modin_df for _ in range(5)])
    big_pandas_df = pandas.concat([pandas_df for _ in range(5)])

    # Check that the constructed Modin DataFrame has virtual partitions when
    # using Ray or Dask, and doesn't when using another execution engines.
    if Engine.get() in ["Ray", "Dask", "Unidist"]:
        assert issubclass(
            type(big_modin_df._query_compiler._modin_frame._partitions[0][0]),
            PandasDataframeAxisPartition,
        )
    else:
        assert not issubclass(
            type(big_modin_df._query_compiler._modin_frame._partitions[0][0]),
            PandasDataframeAxisPartition,
        )
    eval_general(
        big_modin_df, big_pandas_df, lambda df: df.groupby(df.columns[0]).count()
    )


@pytest.mark.parametrize("sort", [True, False])
@pytest.mark.parametrize("is_categorical_by", [True, False])
def test_groupby_sort(sort, is_categorical_by):
    # from issue #3571
    by = np.array(["a"] * 50000 + ["b"] * 10000 + ["c"] * 1000)
    random_state = np.random.RandomState(seed=42)
    random_state.shuffle(by)

    data = {"key_col": by, "data_col": np.arange(len(by))}
    md_df, pd_df = create_test_dfs(data)

    if is_categorical_by:
        md_df = md_df.astype({"key_col": "category"})
        pd_df = pd_df.astype({"key_col": "category"})

    md_grp = md_df.groupby("key_col", sort=sort)
    pd_grp = pd_df.groupby("key_col", sort=sort)

    modin_groupby_equals_pandas(md_grp, pd_grp)
    eval_general(md_grp, pd_grp, lambda grp: grp.sum())
    eval_general(md_grp, pd_grp, lambda grp: grp.size())
    eval_general(md_grp, pd_grp, lambda grp: grp.agg(lambda df: df.mean()))
    eval_general(md_grp, pd_grp, lambda grp: grp.dtypes)
    eval_general(md_grp, pd_grp, lambda grp: grp.first())


def test_sum_with_level():
    data = {
        "A": ["0.0", "1.0", "2.0", "3.0", "4.0"],
        "B": ["0.0", "1.0", "0.0", "1.0", "0.0"],
        "C": ["foo1", "foo2", "foo3", "foo4", "foo5"],
        "D": pandas.bdate_range("1/1/2009", periods=5),
    }
    modin_df, pandas_df = pd.DataFrame(data), pandas.DataFrame(data)
    eval_general(modin_df, pandas_df, lambda df: df.set_index("C").groupby("C").sum())


def test_groupby_with_frozenlist():
    pandas_df = pandas.DataFrame(data={"a": [1, 2, 3], "b": [1, 2, 3], "c": [1, 2, 3]})
    pandas_df = pandas_df.set_index(["a", "b"])
    modin_df = from_pandas(pandas_df)
    eval_general(modin_df, pandas_df, lambda df: df.groupby(df.index.names).count())


@pytest.mark.parametrize(
    "by_func",
    [
        lambda df: "timestamp0",
        lambda df: ["timestamp0", "timestamp1"],
        lambda df: ["timestamp0", df["timestamp1"]],
    ],
)
def test_mean_with_datetime(by_func):
    data = {
        "timestamp0": [pd.to_datetime(1490195805, unit="s")],
        "timestamp1": [pd.to_datetime(1490195805, unit="s")],
        "numeric": [0],
    }

    modin_df, pandas_df = create_test_dfs(data)
    eval_general(modin_df, pandas_df, lambda df: df.groupby(by=by_func(df)).mean())


def test_groupby_ohlc():
    pandas_df = pandas.DataFrame(
        np.random.randint(0, 100, (50, 2)), columns=["stock A", "stock B"]
    )
    pandas_df["Date"] = pandas.concat(
        [pandas.date_range("1/1/2000", periods=10, freq="min").to_series()] * 5
    ).reset_index(drop=True)
    modin_df = pd.DataFrame(pandas_df)
    eval_general(modin_df, pandas_df, lambda df: df.groupby("Date")["stock A"].ohlc())
    pandas_multiindex_result = pandas_df.groupby("Date")[["stock A"]].ohlc()

    with warns_that_defaulting_to_pandas():
        modin_multiindex_result = modin_df.groupby("Date")[["stock A"]].ohlc()
    df_equals(modin_multiindex_result, pandas_multiindex_result)

    pandas_multiindex_result = pandas_df.groupby("Date")[["stock A", "stock B"]].ohlc()
    with warns_that_defaulting_to_pandas():
        modin_multiindex_result = modin_df.groupby("Date")[
            ["stock A", "stock B"]
        ].ohlc()
    df_equals(modin_multiindex_result, pandas_multiindex_result)


def test_groupby_mad_warn():
    modin_df, pandas_df = create_test_dfs(test_groupby_data)
    md_grp = modin_df.groupby(by=modin_df.columns[0])
    pd_grp = pandas_df.groupby(by=pandas_df.columns[0])

    msg = "The 'mad' method is deprecated and will be removed in a future version."
    for grp_obj in (md_grp, pd_grp):
        with pytest.warns(FutureWarning, match=msg):
            grp_obj.mad()


def test_groupby_backfill_warn():
    modin_df = pd.DataFrame(test_groupby_data)
    md_grp = modin_df.groupby(by=modin_df.columns[0])

    msg = "backfill is deprecated and will be removed in a future version."
    with pytest.warns(FutureWarning, match=msg):
        md_grp.backfill()


@pytest.mark.parametrize(
    "modin_df_recipe",
    ["non_lazy_frame", "frame_with_deferred_index", "lazy_frame"],
)
def test_groupby_on_empty_data(modin_df_recipe):
    class ModinDfConstructor:
        def __init__(self, recipe, df_kwargs):
            self._recipe = recipe
            self._mock_obj = None
            self._df_kwargs = df_kwargs

        def non_lazy_frame(self):
            return pd.DataFrame(**self._df_kwargs)

        def frame_with_deferred_index(self):
            df = pd.DataFrame(**self._df_kwargs)
            try:
                # The frame would stop being lazy once index computation is triggered
                df._query_compiler._modin_frame.set_index_cache(None)
            except AttributeError:
                pytest.skip(
                    reason="Selected execution doesn't support deferred indices."
                )

            return df

        def lazy_frame(self):
            donor_obj = pd.DataFrame()._query_compiler

            self._mock_obj = mock.patch(
                f"{donor_obj.__module__}.{donor_obj.__class__.__name__}.lazy_execution",
                new_callable=mock.PropertyMock,
            )
            patch_obj = self._mock_obj.__enter__()
            patch_obj.return_value = True

            df = pd.DataFrame(**self._df_kwargs)
            # The frame is lazy until `self.__exit__()` is called
            assert df._query_compiler.lazy_execution
            return df

        def __enter__(self):
            return getattr(self, self._recipe)()

        def __exit__(self, *args, **kwargs):
            if self._mock_obj is not None:
                self._mock_obj.__exit__(*args, **kwargs)

    def run_test(eval_function, *args, **kwargs):
        df_kwargs = {"columns": ["a", "b", "c"]}
        with ModinDfConstructor(modin_df_recipe, df_kwargs) as modin_df:
            pandas_df = pandas.DataFrame(**df_kwargs)

            modin_grp = modin_df.groupby(modin_df.columns[0])
            pandas_grp = pandas_df.groupby(pandas_df.columns[0])

            eval_function(modin_grp, pandas_grp, *args, **kwargs)

    run_test(eval___getattr__, item="b")
    run_test(eval___getitem__, item="b")
    run_test(eval_agg, func=lambda df: df.mean())
    run_test(eval_aggregate, func=lambda df: df.mean())
    run_test(eval_any)
    run_test(eval_apply, func=lambda df: df.mean())
    run_test(eval_count)
    run_test(eval_cummax)
    run_test(eval_cummin)
    run_test(eval_cumprod)
    run_test(eval_cumsum)
    run_test(eval_dtypes)
    run_test(eval_fillna)
    run_test(eval_groups)
    run_test(eval_len)
    run_test(eval_max)
    run_test(eval_mean)
    run_test(eval_median)
    run_test(eval_min)
    run_test(eval_ndim)
    run_test(eval_ngroup)
    run_test(eval_ngroups)
    run_test(eval_nunique)
    run_test(eval_prod)
    run_test(eval_quantile)
    run_test(eval_rank)
    run_test(eval_size)
    run_test(eval_skew)
    run_test(eval_sum)
    run_test(eval_var)

    if modin_df_recipe != "lazy_frame":
        # TODO: these functions have their specific implementations in the
        # front-end that are unable to operate on empty frames and thus
        # fail on an empty lazy frame.
        # https://github.com/modin-project/modin/issues/5505
        # https://github.com/modin-project/modin/issues/5506
        run_test(eval_pipe, func=lambda df: df.mean())
        run_test(eval_shift)

    # TODO: these functions fail in case of empty data in the pandas itself,
    # we have to modify the `eval_*` functions to be able to check for
    # exceptions equality:
    # https://github.com/modin-project/modin/issues/5441
    # run_test(eval_transform, func=lambda df: df.mean())
    # run_test(eval_std)


def test_skew_corner_cases():
    """
    This test was inspired by https://github.com/modin-project/modin/issues/5545.

    The test verifies that modin acts exactly as pandas when the input data is
    bad for the 'skew' and so some components of the 'skew' formula appears to be invalid:
        ``(count * (count - 1) ** 0.5 / (count - 2)) * (m3 / m2**1.5)``
    """
    # When 'm2 == m3 == 0' thus causing 0 / 0 division in the second multiplier.
    # Note: mX = 'sum((col - mean(col)) ^ x)'
    modin_df, pandas_df = create_test_dfs({"col0": [1, 1, 1], "col1": [10, 10, 10]})
    eval_general(modin_df, pandas_df, lambda df: df.groupby("col0").skew())

    # When 'count < 3' thus causing dividing by zero in the first multiplier
    # Note: count = group_size
    modin_df, pandas_df = create_test_dfs({"col0": [1, 1], "col1": [1, 2]})
    eval_general(modin_df, pandas_df, lambda df: df.groupby("col0").skew())

    # When 'count < 3' and 'm3 / m2 != 0'. The case comes from:
    # https://github.com/modin-project/modin/issues/5545
    modin_df, pandas_df = create_test_dfs({"col0": [1, 1], "col1": [171, 137]})
    eval_general(modin_df, pandas_df, lambda df: df.groupby("col0").skew())


@pytest.mark.parametrize(
    "by",
    [
        pandas.Grouper(key="time_stamp", freq="3D"),
        [pandas.Grouper(key="time_stamp", freq="1M"), "count"],
    ],
)
def test_groupby_with_grouper(by):
    # See https://github.com/modin-project/modin/issues/5091 for more details
    # Generate larger data so that it can handle partitioning cases
    data = {
        "id": [i for i in range(200)],
        "time_stamp": [
            pd.Timestamp("2000-01-02") + datetime.timedelta(days=x) for x in range(200)
        ],
    }
    for i in range(200):
        data[f"count_{i}"] = [i, i + 1] * 100

    modin_df, pandas_df = create_test_dfs(data)
    eval_general(
        modin_df,
        pandas_df,
        lambda df: df.groupby(by).mean(),
    )


def test_groupby_preserves_by_order():
    modin_df, pandas_df = create_test_dfs({"col0": [1, 1, 1], "col1": [10, 10, 10]})

    modin_res = modin_df.groupby([pd.Series([100, 100, 100]), "col0"]).mean()
    pandas_res = pandas_df.groupby([pandas.Series([100, 100, 100]), "col0"]).mean()

    df_equals(modin_res, pandas_res)


@pytest.mark.parametrize(
    "method",
    # test all aggregations from pandas.core.groupby.base.reduction_kernels except
    # nth and corrwith, both of which require extra arguments.
    [
        "all",
        "any",
        "count",
        "first",
        "idxmax",
        "idxmin",
        "last",
        "max",
        "mean",
        "median",
        "min",
        "nunique",
        "prod",
        "quantile",
        "sem",
        "size",
        "skew",
        "std",
        "sum",
        "var",
    ],
)
@pytest.mark.skipif(
    StorageFormat.get() != "Pandas",
    reason="only relevant to pandas execution",
)
def test_groupby_agg_with_empty_column_partition_6175(method):
    df = pd.concat(
        [
            pd.DataFrame({"col33": [0, 1], "index": [2, 3]}),
            pd.DataFrame({"col34": [4, 5]}),
        ],
        axis=1,
    )
    assert df._query_compiler._modin_frame._partitions.shape == (1, 2)
    eval_general(
        df,
        df._to_pandas(),
        lambda df: getattr(df.groupby(["col33", "index"]), method)(),
        # work around https://github.com/modin-project/modin/issues/6016: we don't
        # expect any exceptions.
        raising_exceptions=(Exception,),
    )


def test_groupby_pct_change_diff_6194():
    df = pd.DataFrame(
        {
            "by": ["a", "b", "c", "a", "c"],
            "value": [1, 2, 4, 5, 1],
        }
    )
    # These methods should not crash
    eval_general(
        df,
        df._to_pandas(),
        lambda df: df.groupby(by="by").pct_change(),
    )
    eval_general(
        df,
        df._to_pandas(),
        lambda df: df.groupby(by="by").diff(),
    )
