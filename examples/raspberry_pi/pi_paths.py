"""Source checkout imports and external, private Pi runtime storage."""
import os
from pathlib import Path
import sys

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(REPOSITORY / 'bin'))
DATA_ROOT = Path(os.environ.get('POKEPI_ROOT', str(Path.home() / 'pokemon-pi'))).resolve()
