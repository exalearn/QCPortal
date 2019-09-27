import abc
import distutils
import json
import warnings
from contextlib import contextmanager

from .dataset import Dataset
import pathlib
from typing import Union, List, Tuple
import numpy as np
import pandas as pd
import h5py
from pydantic.fields import Field

class DatasetView(abc.ABC):
    def __init__(self, path: Union[str, pathlib.Path]):
        """
        Parameters
        ----------
        path: Union[str, pathlib.Path]
            File path of view
        """
        if isinstance(path, str):
            path = pathlib.Path(path)
        self._path = path

    @abc.abstractmethod
    def write(self, ds: Dataset) -> None:
        """
        Writes a dataset to disk.

        Parameters
        ----------
        ds: Dataset
            The dataset to write.

        Returns
        -------
            None
        """
        pass

    @abc.abstractmethod
    def list_values(self) -> pd.DataFrame:
        """
        Get a list of all available value columns.

        Returns
        -------
            A Dataframe with specification of available columns.
        """
        pass

    @abc.abstractmethod
    def get_values(self, queries: List[Tuple[str]]) -> Tuple[pd.DataFrame, List[str]]:
        """
        Get value columns.

        Parameters
        ----------
        queries: List[Tuple[str]]
            List of column metadata to match.

        Returns
        -------
            A Dataframe whose columns correspond to each query and a list of units for each column.
        """
        pass


class HDF5View(DatasetView):

    def __init__(self, path: Union[str, pathlib.Path]):
        super().__init__(path)

    def list_values(self) -> pd.DataFrame:
        df = pd.DataFrame()
        with self._read_file() as f:
            history_keys = json.loads(f.attrs['history_keys'])
            for dataset in f['value'].values():
                row = {k: json.loads(dataset.attrs[k]) for k in history_keys}
                row["name"] = json.loads(dataset.attrs["name"])
                row["native"] = True
                df = df.append(row, ignore_index=True)
            for dataset in f['contributed_value'].values():
                # TODO: duplicate
                row = dict()
                row["name"] = json.loads(dataset.attrs["name"])
                for k in history_keys:
                    row[k] = "Unknown"
                # ReactionDataset uses "default" as a default value for stoich, but many contributed datasets lack a stoich field
                if "stoichiometry" in history_keys:
                    row["stoichiometry"] = "default"
                if "theory_level_details" in dataset.attrs:
                    theory_level_details = json.loads(dataset.attrs["theory_level_details"])
                    if isinstance(theory_level_details, dict):
                        row.update(**theory_level_details)
                row["native"] = False
                print(row)
                df = df.append(row, ignore_index=True)
        print(df)
        return df

    def get_values(self, queries: List[Tuple[str]]) -> Tuple[pd.DataFrame, List[str]]:
        units = {}
        with self._read_file() as f:
            ret = pd.DataFrame(index=f["entry"][:])

            for query in queries:
                dataset_name = "value/" if query["native"] else "contributed_value/"
                dataset_name += self._normalize_hdf5_name(query["name"])
                driver = query["driver"]

                dataset = f[dataset_name]
                if not h5py.check_dtype(vlen=dataset.dtype):
                    data = list(dataset[:])
                else:
                    nentries = dataset.shape[0]
                    if driver.lower() == "gradient":
                        data = [np.reshape(dataset[i], (-1, 3)) for i in range(nentries)]
                    elif driver.lower() == "hessian":
                        data = []
                        for i in range(nentries):
                            n2 = len(dataset[i])
                            n = int(round(np.sqrt(n2)))
                            data.append(np.reshape(dataset[i], (n, n)))
                    else:
                        warnings.warn(f"Variable length data type not understood, returning flat array "
                                      f"(driver = {driver}).", RuntimeWarning)
                        data = list(dataset[:])
                column_name = query["name"]
                column_units = json.loads(dataset.attrs["units"])
                ret[column_name] = data
                units[column_name] = column_units

        return ret, units

    def write(self, ds: Dataset):
        # For data checksums
        dataset_kwargs = {"chunks": True,
                          "fletcher32": True}

        n_records = len(ds.data.records)
        default_shape = (n_records,)

        if h5py.__version__ >= distutils.version.StrictVersion("2.9.0"):
            vlen_double_t = h5py.vlen_dtype(np.dtype("float64"))
            utf8_t = h5py.string_dtype(encoding="utf-8")
            vlen_utf8_t = h5py.vlen_dtype(utf8_t)
        else:
            vlen_double_t = h5py.special_dtype(vlen=np.dtype("float64"))
            utf8_t = h5py.special_dtype(vlen=str)
            vlen_utf8_t = h5py.special_dtype(vlen=utf8_t)

        driver_dataspec = {"energy": {"dtype": np.dtype("float64"), "shape": default_shape},
                           "gradient": {"dtype": vlen_double_t, "shape": default_shape},
                           "hessian": {"dtype": vlen_double_t, "shape": default_shape},
                           "dipole": {"dtype": np.dtype("float64"), "shape": (n_records, 3)}
                           }

        def _write_dataset(dataset, column, entry_dset):
            assert column.shape[1] == 1
            for i, name in enumerate(entry_dset):
                element = column.loc[name][0]
                if not h5py.check_dtype(vlen=dataset.dtype):
                    dataset[i] = element
                # Variable length datatypes require flattening of the array and special handling of missing values
                else:
                    try:
                        dataset[i] = element.ravel()
                    except AttributeError:
                        if np.isnan(element):
                            pass
                        else:
                            raise

        def _serialize_fields(datamodel, names):
            """ Convert a pydantic datamodel into strings for storage in HDF5 metadata """
            # TODO: Can I use elemental for this?
            for name in names:
                yield name, json.dumps(getattr(datamodel, name))

        with self._write_file() as f:
            ## TODO: save some info about the server
            # Collection attributes
            f.attrs.update(_serialize_fields(ds.data,  {"name", "collection", "provenance", "tagline", "tags", "id", "history_keys"}))

            # Export entries
            entry_dset = f.create_dataset("entry", shape=default_shape, dtype=utf8_t, **dataset_kwargs)
            entry_dset[:] = ds.get_index()

            # Export native data columns
            value_group = f.create_group("value")
            history = ds.list_values(native=True, force=True).reset_index().to_dict("records")
            for specification in history:
                name = specification.pop("name")
                dataset_name = self._normalize_hdf5_name(name)
                df = ds.get_values(**specification, force=True)
                specification["name"] = name
                driver = specification["driver"]

                assert df.shape[1] == 1

                dataspec = driver_dataspec[driver]
                dataset = value_group.create_dataset(dataset_name, **dataspec, **dataset_kwargs)

                for key in specification:
                    dataset.attrs[key] = json.dumps(specification[key])
                dataset.attrs["units"] = json.dumps(ds.units)

                _write_dataset(dataset, df, entry_dset)

            # Export contributed data columns
            contributed_group = f.create_group("contributed_value")
            for cv_name in ds.list_values(force=True, native=False)["name"]:
                cv_df = ds.get_values(name=cv_name, force=True, native=False)
                cv_model = ds.data.contributed_values[cv_name.lower()]

                try:
                    dataspec = driver_dataspec[cv_model.theory_level_details["driver"]]
                except (KeyError, TypeError):
                    warnings.warn(
                        f"Contributed values column {cv_name} does not provide driver in theory_level_details. "
                        f"Assuming default driver for the dataset ({ds.data.default_driver}).")
                    dataspec = driver_dataspec[ds.data.default_driver]

                dataset = contributed_group.create_dataset(self._normalize_hdf5_name(cv_name),
                                                           **dataspec, **dataset_kwargs)
                dataset.attrs.update(
                        _serialize_fields(cv_model, {"name", "theory_level", "units", "doi", "comments", "theory_level", "theory_level_details"}))

                _write_dataset(dataset, cv_df, entry_dset)

    @staticmethod
    def _normalize_hdf5_name(name: str) -> str:
        """ Handles names with / in them, which is disallowed in HDF5 """
        if ":" in name:
            raise ValueError("':' not allowed in names")
        return name.replace("/", ":")

    @contextmanager
    def _read_file(self):
        yield h5py.File(self._path, 'r')

    @contextmanager
    def _write_file(self):
        yield h5py.File(self._path, 'w')
