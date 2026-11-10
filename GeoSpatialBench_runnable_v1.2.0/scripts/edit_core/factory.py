"""The 28 formal relation IDs and editing operation constants."""
# GeoFM v5 formal L1: 28 active relations. Deferred: D03, D07, O07, O09.
CORE_28_IDS = (
    "T01", "T02", "T03", "T04", "T05", "T06", "T08", "T09", "T11",
    "D01", "D02", "D04", "D05", "D06",
    "M01", "M02", "M04", "M08", "M12", "M14", "M15", "M18",
    "O01", "O02", "O03", "O04", "O06", "O08",
)
DEFERRED_IDS = ("D03", "D07", "O07", "O09")

CARDINAL_4 = ["N", "E", "S", "W"]
CARDINAL_8 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
_ANGLES = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5,
    "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
    "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}

EDIT_OPS = ["translate", "rotate", "scale", "snap", "attribute_edit"]

