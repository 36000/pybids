""" File-indexing functionality. """

import os
import re
import json
from keyword import iskeyword
import warnings
from copy import deepcopy
from collections import defaultdict, namedtuple

from .writing import build_path, write_contents_to_file
from .models import Config, BIDSFile, Entity, Tag, FileAssociation
from ..utils import listify, check_path_matches_patterns
from ..config import get_option
from ..external import six


def _extract_entities(bidsfile, entities):
    match_vals = {}
    for e in entities.values():
        m = e.match_file(bidsfile)
        if m is None and e.mandatory:
            break
        if m is not None:
            match_vals[e.name] = (e, m)
    return match_vals


def index_layout(layout, force_index=None, index_metadata=True):

    session = layout.session
    root_path = layout.root
    config = list(layout.config.values())

    def _index_file(f, dirpath, entities):
        ''' Create DB record for file and its tags. '''
        abs_fn = os.path.join(dirpath, f)

        # Skip files that fail validation, unless forcibly indexing
        if not force_index and not layout._validate_file(abs_fn):
            return None

        bf = BIDSFile(abs_fn)
        session.add(bf)

        # Extract entity values
        match_vals = {}
        for e in entities.values():
            m = e.match_file(bf)
            if m is None and e.mandatory:
                break
            if m is not None:
                match_vals[e.name] = (e, m)

        # Create Entity <=> BIDSFile mappings
        if match_vals:
            for _, (ent, val) in match_vals.items():
                tag = Tag(bf, ent, str(val), ent._dtype)
                session.add(tag)

        return bf

    def index_dir(path, config, parent=None, force_index=False):

        abs_path = os.path.join(root_path, path)
        config = list(config)     # Shallow copy

        # Check for additional config file in directory
        layout_file = layout.config_filename
        config_file = os.path.join(abs_path, layout_file)
        if os.path.exists(config_file):
            cfg = Config.load(config_file, session=session)
            config.append(cfg)

        # Track which entities are valid in filenames for this directory
        config_entities = {}
        for c in config:
            config_entities.update(c.entities)

        # JSONFile = namedtuple('JSONFile', ['entities', 'payload', 'parent'])

        for (dirpath, dirnames, filenames) in os.walk(path):

            # If layout configuration file exists, delete it
            if layout.config_filename in filenames:
                filenames.remove(layout.config_filename)

            for f in filenames:

                bf = _index_file(f, dirpath, config_entities)
                if bf is None:
                    continue

            session.commit()

            # Recursively index subdirectories
            for d in dirnames:

                d = os.path.join(dirpath, d)

                # Derivative directories must always be added separately and
                # passed as their own root, so terminate if passed.
                if d.startswith(os.path.join(layout.root, 'derivatives')):
                    continue

                # Skip directories that fail validation, unless force_index
                # is defined, in which case we have to keep scanning, in the
                # event that a file somewhere below the current level matches.
                # Unfortunately we probably can't do much better than this
                # without a lot of additional work, because the elements of
                # .force_index can be SRE_Patterns that match files below in
                # unpredictable ways.
                if check_path_matches_patterns(d, layout.force_index):
                    force_index = True
                else:
                    valid_dir = layout._validate_dir(d)
                    # Note the difference between self.force_index and
                    # self.layout.force_index.
                    if not valid_dir and not layout.force_index:
                        continue

                index_dir(d, list(config), path, force_index)

            # prevent subdirectory traversal
            break
    
    index_dir(root_path, config, None, force_index)

    if index_metadata:
        _index_metadata(layout)


def _index_metadata(layout):
    # Process JSON files first if we're indexing metadata

    session = layout.session
    all_files = layout.get(absolute_paths=True)

    # Track ALL entities we've seen in file names or metadatas
    all_entities = {}
    for c in layout.config.values():
        all_entities.update(c.entities)

    # We build up a store of all file data as we iterate files. It looks like:
    # { extension/suffix: dirname: [(entities, payload)]}}. The payload
    # is left empty for non-JSON files.
    file_data = {}

    for bf in all_files:
        file_ents = bf.entities.copy()
        suffix = file_ents.pop('suffix', None)
        ext = file_ents.pop('extension', None)

        if suffix is not None and ext is not None:
            key = "{}/{}".format(ext, suffix)
            if key not in file_data:
                file_data[key] = defaultdict(list)

            if ext == 'json':
                with open(bf.path, 'r') as handle:
                    payload = json.load(handle)
            else:
                payload = None

            to_store = (file_ents, payload, bf.path)
            file_data[key][bf.dirname].append(to_store)


    filenames = [bf for bf in all_files if not bf.path.endswith('.json')]
    for bf in filenames:
        file_ents = bf.entities.copy()
        suffix = file_ents.pop('suffix', None)
        ext = file_ents.pop('extension', None)
        file_ent_keys = set(file_ents.keys())

        if suffix is None or ext is None:
            continue

        # Extract metadata associated with the file. The idea is
        # that we loop over parent directories, and if we find
        # payloads in the file_data store (indexing by directory
        # and current file suffix), we check to see if the
        # candidate JS file's entities are entirely consumed by
        # the current file. If so, it's a valid candidate, and we
        # add the payload to the stack. Finally, we invert the
        # stack and merge the payloads in order.
        ext_key = "{}/{}".format(ext, suffix)
        json_key = "json/{}".format(suffix)
        dirname = bf.dirname

        payloads = []
        ancestors = []

        while True:
            # Get JSON payloads
            json_data = file_data.get(json_key, {}).get(dirname, [])
            for js_ents, js_md, js_path in json_data:
                js_keys = set(js_ents.keys())
                if (js_keys - file_ent_keys):
                    continue
                matches = [js_ents[name] == file_ents[name]
                            for name in js_keys]
                if all(matches):
                    payloads.append((js_md, js_path))

            # Get all files this file inherits from
            candidates = file_data.get(ext_key, {}).get(dirname, [])
            for ents, _, path in candidates:
                keys = set(ents.keys())
                if (keys - file_ent_keys):
                    continue
                matches = [ents[name] == file_ents[name] for name in keys]
                if all(matches):
                    ancestors.append(path)

            parent = os.path.dirname(dirname)
            if parent == dirname:
                break
            dirname = parent

        if not payloads:
            continue

        # Create DB records for metadata associations
        js_file = payloads[-1][1]
        associations = [
            FileAssociation(src=js_file, dst=bf.path, kind='Metadata'),
            FileAssociation(src=bf.path, dst=js_file, kind='Metadata')
        ]
        session.add_all(associations)

        # Consolidate metadata for file by looping over inherited JSON files
        file_md = {}
        for pl, js_file in payloads[::-1]:
            file_md.update(pl)

        # Create FileAssociation records for JSON inheritance
        n_pl = len(payloads)
        for i, (pl, js_file) in enumerate(payloads):
            if (i + 1) < n_pl:
                other = payloads[i+1][1]
                associations = [
                    FileAssociation(src=js_file, dst=other, kind='Child'),
                    FileAssociation(src=other, dst=js_file, kind='Parent')
                ]
                session.add_all(associations)

        # Inheritance for current file
        n_pl = len(ancestors)
        for i, src in enumerate(ancestors):
            if (i + 1) < n_pl:
                dst = ancestors[i+1]
                associations = [
                    FileAssociation(src=src, dst=dst, kind='Child'),
                    FileAssociation(src=dst, dst=src, kind='Parent')
                ]
                session.add_all(associations)

        # Create database records, including any new Entities
        for md_key, md_val in file_md.items():
            if md_key not in all_entities:
                all_entities[md_key] = Entity(md_key, is_metadata=True)
                session.add(all_entities[md_key])
            tag = Tag(bf, all_entities[md_key], md_val)
            session.add(tag)

        if len(session.new) >= 1000:
            session.commit()

    session.commit()
