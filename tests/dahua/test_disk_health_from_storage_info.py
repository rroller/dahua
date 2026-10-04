"""parse_storage_disks turns getDeviceAllInfo into per-disk health + capacity (#745).

The field shapes are the ones captured from a DHI-NVR5464: the CGI parser preserves
the device's flat keys verbatim (`list.info[0].Name`, `.State`, `.HealthDataFlag`,
`.Detail[M].IsError` / `.TotalBytes` / `.UsedBytes`), bytes arrive as strings like
"1495797858304.000000", and SMART attributes are not in the answer at all -- they are
not served by any API on the recorders measured.
"""

from custom_components.dahua.client import parse_storage_disks

# Three partitions at one size, one smaller -- a real sda from the NVR, all full.
PART = [
    "1495797858304.000000",
    "1495797858304.000000",
    "1495797858304.000000",
    "1435787853824.000000",
]
TOTAL = sum(int(float(x)) for x in PART)


def _disk(index, name, state="Success", flag="0", error_partition=None):
    out = {
        "list.info[%d].Name" % index: name,
        "list.info[%d].State" % index: state,
        "list.info[%d].HealthDataFlag" % index: flag,
    }
    for m, size in enumerate(PART):
        out["list.info[%d].Detail[%d].IsError" % (index, m)] = (
            "true" if error_partition == m else "false"
        )
        out["list.info[%d].Detail[%d].TotalBytes" % (index, m)] = size
        out["list.info[%d].Detail[%d].UsedBytes" % (index, m)] = size
        out["list.info[%d].Detail[%d].Path" % (index, m)] = "%s%d" % (name, m)
    return out


def test_a_healthy_disk_is_parsed_with_summed_capacity():
    disks = parse_storage_disks(_disk(0, "/dev/sda"))

    assert len(disks) == 1
    disk = disks[0]
    assert disk["name"] == "/dev/sda"
    assert disk["state"] == "Success"
    assert disk["healthy"] is True
    assert disk["has_error"] is False
    assert disk["total_bytes"] == TOTAL
    assert disk["used_bytes"] == TOTAL
    assert disk["health_flag"] == 0


def test_disks_come_back_in_index_order():
    data = {}
    data.update(_disk(0, "/dev/sda"))
    data.update(_disk(1, "/dev/sdb"))

    assert [d["name"] for d in parse_storage_disks(data)] == ["/dev/sda", "/dev/sdb"]


def test_a_partition_error_makes_the_disk_unhealthy():
    disk = parse_storage_disks(_disk(0, "/dev/sdc", error_partition=2))[0]

    assert disk["has_error"] is True
    assert disk["healthy"] is False


def test_a_state_other_than_success_is_unhealthy():
    disk = parse_storage_disks(_disk(0, "/dev/sdd", state="Error"))[0]

    assert disk["healthy"] is False


def test_unrelated_or_empty_data_yields_no_disks():
    assert parse_storage_disks({}) == []
    assert parse_storage_disks({"table.General.MachineName": "NVR"}) == []


def test_an_entry_with_no_name_is_dropped():
    assert parse_storage_disks({"list.info[0].State": "Success"}) == []
