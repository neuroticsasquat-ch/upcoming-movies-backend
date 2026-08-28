"""The sweep: the scheduled pass that discovers and maintains the undated films TMDB's
dated `/discover/movie` roster cannot reach (spec §3, §6)."""

from upmovies.ingest.sweep.admission import AdmissionTranches as AdmissionTranches
from upmovies.ingest.sweep.credit_events import AttachedCredit as AttachedCredit
from upmovies.ingest.sweep.credit_events import (
    CreditDetachmentResult as CreditDetachmentResult,
)
from upmovies.ingest.sweep.credit_events import CreditEventResult as CreditEventResult
from upmovies.ingest.sweep.credit_events import CreditGroup as CreditGroup
from upmovies.ingest.sweep.credit_events import credit_role as credit_role
from upmovies.ingest.sweep.credit_events import group_attachments as group_attachments
from upmovies.ingest.sweep.credit_events import (
    run_credit_attachment_events as run_credit_attachment_events,
)
from upmovies.ingest.sweep.credit_events import (
    run_credit_detachment_events as run_credit_detachment_events,
)
from upmovies.ingest.sweep.enumerate_phase import EnumerateResult as EnumerateResult
from upmovies.ingest.sweep.enumerate_phase import run_sweep_enumerate as run_sweep_enumerate
from upmovies.ingest.sweep.field_events import CatalogFieldEvent as CatalogFieldEvent
from upmovies.ingest.sweep.field_events import FieldEventResult as FieldEventResult
from upmovies.ingest.sweep.field_events import TrackedChange as TrackedChange
from upmovies.ingest.sweep.field_events import classify_field_change as classify_field_change
from upmovies.ingest.sweep.field_events import run_field_change_events as run_field_change_events
from upmovies.ingest.sweep.refresh_phase import RefreshResult as RefreshResult
from upmovies.ingest.sweep.refresh_phase import run_sweep_refresh as run_sweep_refresh
from upmovies.ingest.sweep.release_events import (
    ReleaseEventResult as ReleaseEventResult,
)
from upmovies.ingest.sweep.release_events import (
    run_release_date_events as run_release_date_events,
)
from upmovies.ingest.sweep.seeds import CandidateTally as CandidateTally
from upmovies.ingest.sweep.seeds import SeedAttachment as SeedAttachment
from upmovies.ingest.sweep.seeds import load_known_film_tmdb_ids as load_known_film_tmdb_ids
from upmovies.ingest.sweep.seeds import load_seed_person_ids as load_seed_person_ids
from upmovies.ingest.sweep.seeds import seed_attachments as seed_attachments
from upmovies.ingest.sweep.seeds import tally_attachments as tally_attachments
from upmovies.ingest.sweep.summary import sweep_detail as sweep_detail
