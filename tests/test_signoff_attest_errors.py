"""Public exception identity is shared by attestation helpers and older workers."""

import pickle

from klayout_tools import signoff_attest, signoff_base


def test_attestation_error_keeps_its_public_pickle_path():
    error = signoff_base.AttestationError("invalid evidence")
    assert type(error) is signoff_attest.AttestationError
    assert isinstance(error, signoff_attest.SignoffError)
    # Protocol 0 names the public module explicitly, as older workers expect.
    data = pickle.dumps(error, protocol=0)
    assert b"cklayout_tools.signoff_attest\nAttestationError\n" in data
