"""The sweep: the scheduled pass that discovers and maintains the undated films TMDB's
dated `/discover/movie` roster cannot reach (spec §3, §6)."""

from upmovies.ingest.sweep.admission import AdmissionTranches as AdmissionTranches
from upmovies.ingest.sweep.enumerate_phase import EnumerateResult as EnumerateResult
from upmovies.ingest.sweep.enumerate_phase import run_sweep_enumerate as run_sweep_enumerate
from upmovies.ingest.sweep.seeds import CandidateTally as CandidateTally
from upmovies.ingest.sweep.seeds import SeedAttachment as SeedAttachment
from upmovies.ingest.sweep.seeds import load_known_film_tmdb_ids as load_known_film_tmdb_ids
from upmovies.ingest.sweep.seeds import load_seed_person_ids as load_seed_person_ids
from upmovies.ingest.sweep.seeds import seed_attachments as seed_attachments
from upmovies.ingest.sweep.seeds import tally_attachments as tally_attachments
