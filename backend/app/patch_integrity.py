"""Canonical patch identity helpers shared by validation and publication."""

from hashlib import sha256

from app.patch_schemas import PatchProposal


def shoplite_diff_hash(proposal: PatchProposal) -> str:
    """Hash the supported one-file ShopLite edit in a stable, scoped format."""
    if (
        len(proposal.files) != 1
        or proposal.files[0].path != "styles/checkout.css"
        or len(proposal.files[0].edits) != 1
    ):
        raise ValueError("ShopLite diff identity requires exactly one checkout CSS edit")
    edit = proposal.files[0].edits[0]
    material = f"demo-app/styles/checkout.css\0{edit.old_text}\0{edit.new_text}".encode("utf-8")
    return sha256(material).hexdigest()
