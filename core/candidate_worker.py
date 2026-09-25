"""Fixed workflow subprocess entry point; only the host supervisor invokes this."""

import json
import sys

from .database import Database
from .self_edit import Limits, SelfEditService


def main():
    config = json.loads(sys.stdin.read(32000))
    database = Database(config['database'])
    try:
        service = SelfEditService(database, source_root=config['source'], candidate_root=config['root'],
                                  limits=Limits(**config['limits']))
        service._work(config['candidate_id'], config['action'])
    finally:
        database.close()


if __name__ == '__main__':
    main()
