from pathlib import Path
from typing import Optional

from no_hassle_kv import DbDict
import shelve
import pickle

from no_hassle_kv.DbOffsetStorage import DbOffsetStorage
from no_hassle_kv.CompactStorage import CompactStorage
import mmap


class CompactKeyValueStore:
    file_index = None
    index = None
    key_map = None

    opened_shards = None
    shard_for_write = 0
    written_in_current_shard = 0
    shard_size = 0

    def __init__(self, path, shard_size=2**30, **kwargs):
        self.path = Path(path)

        self.initialize_file_index(shard_size, **kwargs)
        self.initialize_offset_index(**kwargs)

    def initialize_file_index(self, shard_size, **kwargs):
        self.file_index = dict()  # (shard, filename)
        self.opened_shards = dict()  # (shard, file, mmap object) if mmap is none -> opened for write
        self.shard_for_write = 0
        self.written_in_current_shard = 0
        self.shard_size = shard_size

    def initialize_offset_index(self, **kwargs):
        self.key_map = dict()
        self.index = CompactStorage(3, dtype="L")  # third of space is wasted to shards

    def init_storage(self, size):
        self.index.active_storage_size = size

    def __setitem__(self, key, value, key_error='ignore'):
        if self.key_map is not None:
            if key not in self.key_map:
                self.key_map[key] = len(self.index)
            key_: int = self.key_map[key]
        else:
            if not isinstance(key, int):
                raise ValueError("Keys should be integers when setting compact_ensured=True")
            key_: int = key

        serialized = pickle.dumps(value, protocol=4)

        try:
            existing_shard, existing_pos, existing_len = self.index[key_]  # check if there is an entry with such key
        except IndexError:
            pass
        else:
            if len(serialized) == existing_len:
                # successfully retrieved existing position and can overwrite old data
                _, mm = self.reading_mode(existing_shard)
                mm[existing_pos: existing_pos + existing_len] = serialized
                return

        # no old data or the key is new
        f, _ = self.writing_mode(self.shard_for_write)
        position = f.tell()
        written = f.write(serialized)
        self.index.append((self.shard_for_write, position, written))
        self.increment_byte_count(written)

    # def add_posting(self, term_id, postings):
    #     if self.index is None:
    #         raise Exception("Index is not initialized")
    #
    #     serialized = pickle.dumps(postings, protocol=4)
    #
    #     f, _ = self.writing_mode(self.shard_for_write)
    #
    #     position = f.tell()
    #     written = f.write(serialized)
    #     self.index[term_id] = (self.shard_for_write, position, written)
    #     self.increment_byte_count(written)
    #     return term_id

    def increment_byte_count(self, written):
        self.written_in_current_shard += written
        if self.written_in_current_shard >= self.shard_size:
            self.shard_for_write += 1
            self.written_in_current_shard = 0

    def __getitem__(self, key):
        if self.key_map is not None:
            if key not in self.key_map:
                raise KeyError("Key does not exist:", key)
            key_ = self.key_map[key]
        else:
            key_ = key
            if key_ >= len(self.index):
                raise KeyError("Key does not exist:", key)
        try:
            return self.get_with_id(key_)
        except ValueError:
            raise KeyError("Key does not exist:", key)

    def get_with_id(self, doc_id):
        triplet = self.index[doc_id]
        if type(triplet) is None:
            raise KeyError("Key not found: ", doc_id)
        shard, pos, len_ = triplet
        if len_ == 0:
            raise ValueError("Entry length is 0")
        _, mm = self.reading_mode(shard)
        return pickle.loads(mm[pos: pos+len_])

    def get_name_format(self, id_):
        return 'store_shard_{0:04d}'.format(id_)

    def open_for_read(self, name):
        # raise file not exists
        f = open(self.path.joinpath(name), "r+b")
        mm = mmap.mmap(f.fileno(), 0)
        return f, mm

    def open_for_write(self, name):
        # raise file not exists
        self.check_dir_exists()
        f = open(self.path.joinpath(name), "ab")
        return f, None

    def check_dir_exists(self):
        if not self.path.is_dir():
            self.path.mkdir()

    def writing_mode(self, id_):
        if id_ not in self.opened_shards:
            if id_ not in self.file_index:
                self.file_index[id_] = self.get_name_format(id_)
            self.opened_shards[id_] = self.open_for_write(self.file_index[id_])
        elif self.opened_shards[id_][1] is not None:  # mmap is None
            self.opened_shards[id_][1].close()
            self.opened_shards[id_][0].close()
            self.opened_shards[id_] = self.open_for_write(self.file_index[id_])
        return self.opened_shards[id_]

    def reading_mode(self, id_):
        if id_ not in self.opened_shards:
            if id_ not in self.file_index:
                self.file_index[id_] = self.get_name_format(id_)
            self.opened_shards[id_] = self.open_for_read(self.file_index[id_])
        elif self.opened_shards[id_][1] is None:
            self.opened_shards[id_][0].close()
            self.opened_shards[id_] = self.open_for_read(self.file_index[id_])
        return self.opened_shards[id_]

    def save_param(self):
        pickle.dump((
            self.file_index,
            self.shard_for_write,
            self.written_in_current_shard,
            self.shard_size,
            self.path,
            self.key_map
        ), open(self.path.joinpath("store_params"), "wb"), protocol=4)

    def load_param(self):
        self.file_index,\
            self.shard_for_write,\
            self.written_in_current_shard,\
            self.shard_size,\
            self.path, \
            self.key_map = pickle.load(open(self.path.joinpath("store_params"), "rb"))

    def save_index(self):
        pickle.dump(self.index, open(self.path.joinpath("store_index"), "wb"), protocol=4)

    def load_index(self):
        self.index = pickle.load(open(self.path.joinpath("store_index"), "rb"))

    def save(self):
        self.save_index()
        self.save_param()
        self.close_all_shards()

    @classmethod
    def load(cls, path):
        store = CompactKeyValueStore(path)
        store.load_param()
        store.load_index()
        return store

    def close_all_shards(self):
        for shard in self.opened_shards.values():
            for s in shard[::-1]:
                if s:
                    s.close()

    def close(self):
        self.close_all_shards()

    def commit(self):
        for shard in self.opened_shards.values():
            if shard[1] is not None:
                shard[1].flush()


class KVStore(CompactKeyValueStore):
    def __init__(self, path, shard_size=2 ** 30, index_backend: Optional[str] = "sqlite", **kwargs):
        """
        Create a disk backed key-value storage.
        :param path: Location on the disk.
        :param shard_size: Size of storage partition in bytes.
        :param index_backend: Backend for storing the index. Available backends are `shelve` and `sqlite`. `shelve` is
            based on Python's shelve library. It relies on key hashing and collisions are possible. Additionally,
            `shelve` storage occupies more space on disk. There is no collisions with `sqlite`, but key value is must
            be string.
        """
        super().__init__(path, shard_size, index_backend="sqlite", **kwargs)
        self.check_dir_exists()

    def initialize_offset_index(self, index_backend="sqlite", **kwargs):
        if index_backend is None:
            index_backend = self.infer_backend()
        self.create_index(self.get_index_path(index_backend))

    def infer_backend(self):
        if self.path.joinpath("store_index.shelve.db").is_file():
            return "shelve"
        elif self.path.joinpath("store_index.s3db").is_file():
            return "sqlite"
        else:
            raise FileNotFoundError("No index file found.")

    def get_index_path(self, index_backend):
        if index_backend == "shelve":
            index_path = self.path.joinpath("store_index.shelve")
        elif index_backend == "sqlite":
            index_path = self.path.joinpath("store_index.s3db")
        else:
            raise ValueError(f"`index_backend` should be `shelve` or `sqlite`, but `{index_backend}` is provided.")
        return index_path

    def create_index(self, index_path):

        parent = index_path.parent
        if not parent.is_dir():
            parent.mkdir()

        if index_path.name.endswith(".shelve"):
            self.index = shelve.open(index_path, protocol=4)
        else:
            self.index = DbOffsetStorage(index_path)
            # self.index = DbDict(index_path)

    def __setitem__(self, key, value, key_error='ignore'):
        if type(self.index) is DbDict:
            if type(key) is not str:
                raise TypeError(
                    f"Key type should be `str` when `sqlite` is used for index backend, but {type(key)} given."
                )
        serialized = pickle.dumps(value, protocol=4)

        # try:
        #     existing_shard, existing_pos, existing_len = self.index[key] # check if there is an entry with such key
        # except KeyError:
        #     pass
        # else:
        #     if len(serialized) == existing_len:
        #         # successfully retrieved existing position and can overwrite old data
        #         _, mm = self.reading_mode(existing_shard)
        #         mm[existing_pos: existing_pos + existing_len] = serialized
        #         return

        # no old data or the key is new
        f, _ = self.writing_mode(self.shard_for_write)
        position = f.tell()
        written = f.write(serialized)

        self.index[key] = (self.shard_for_write, position, written)
        self.increment_byte_count(written)

    def __getitem__(self, key):
        if type(self.index) is DbDict:
            if type(key) is not str:
                raise TypeError(
                    f"Key type should be `str` when `sqlite` is used for index backend, but {type(key)} given."
                )
        return self.get_with_id(key)

    def save(self):
        self.commit()
        self.save_param()
        self.close_all_shards()

    def save_index(self):
        pass

    def commit(self):
        super(KVStore, self).commit()
        if type(self.index) in {DbDict, DbOffsetStorage}:
            self.index.commit()  # for DbDict index
        else:
            self.index.sync()

    @classmethod
    def load(cls, path):
        store = KVStore(path, index_backend=None)
        store.load_param()
        return store

    def load_index(self):
        pass