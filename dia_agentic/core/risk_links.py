"""Connect vendors and approvers that recur across independent anomaly clusters."""
from collections import defaultdict

from dia_agentic.core.evidence import parse_amount


def _vendor(row):
    number = str(row.get("vendor_number") or "").strip()
    name = str(row.get("vendor_name") or "").strip()
    if not number and not name:
        return None
    key = number.lower() if number else name.lower()
    label = f"{name} ({number})" if name and number else (name or number)
    return key, label


def _approver(row):
    value = str(row.get("authorized_by") or "").strip()
    return (value.lower(), value) if value else None


def build_connected_risks(rows: list[dict], clusters: list[dict], limit: int = 12) -> list[dict]:
    """Return entities present in at least two clusters and annotate each cluster."""
    entities = {
        "vendor": defaultdict(lambda: {"labels": defaultdict(int), "clusters": set(), "rows": set()}),
        "approver": defaultdict(lambda: {"labels": defaultdict(int), "clusters": set(), "rows": set()}),
    }

    for cluster in clusters:
        cluster_id = cluster["cluster_id"]
        for idx in set(cluster.get("row_indices", [])):
            if idx < 0 or idx >= len(rows):
                continue
            row = rows[idx]
            for entity_type, getter in (("vendor", _vendor), ("approver", _approver)):
                entity = getter(row)
                if not entity:
                    continue
                key, label = entity
                item = entities[entity_type][key]
                item["labels"][label] += 1
                item["clusters"].add(cluster_id)
                item["rows"].add(idx)

    title_by_id = {c["cluster_id"]: c["title"] for c in clusters}
    connected = []
    for entity_type, values in entities.items():
        for key, item in values.items():
            if len(item["clusters"]) < 2:
                continue
            row_indices = sorted(item["rows"])
            cluster_ids = sorted(item["clusters"])
            connected.append({
                "entity_type": entity_type,
                "entity_key": key,
                "label": max(item["labels"], key=item["labels"].get),
                "cluster_count": len(cluster_ids),
                "invoice_count": len(row_indices),
                "amount_at_risk": round(sum(
                    parse_amount(rows[i].get("amount_excl_lc")) for i in row_indices
                ), 2),
                "cluster_ids": cluster_ids,
                "cluster_titles": [title_by_id[cid] for cid in cluster_ids if cid in title_by_id],
            })

    connected.sort(key=lambda x: (-x["cluster_count"], -x["amount_at_risk"], -x["invoice_count"]))
    connected = connected[:limit]

    by_cluster = defaultdict(list)
    for item in connected:
        compact = {
            "entity_type": item["entity_type"],
            "label": item["label"],
            "cluster_count": item["cluster_count"],
        }
        for cluster_id in item["cluster_ids"]:
            by_cluster[cluster_id].append(compact)
    for cluster in clusters:
        cluster["connected_entities"] = by_cluster.get(cluster["cluster_id"], [])[:3]

    return connected
