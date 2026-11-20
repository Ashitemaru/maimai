"""The one list of Japanese maimai versions, oldest first, with their version codes.

Codes are the lower bounds of numeric JP version codes. Add a line when a new version
releases; until then charts citing an unlisted version wait for a manual decision.
"""

JP_VERSIONS = (
    (10000, "maimai"),
    (11000, "maimai PLUS"),
    (12000, "GreeN"),
    (13000, "GreeN PLUS"),
    (14000, "ORANGE"),
    (15000, "ORANGE PLUS"),
    (16000, "PiNK"),
    (17000, "PiNK PLUS"),
    (18000, "MURASAKi"),
    (18500, "MURASAKi PLUS"),
    (19000, "MiLK"),
    (19500, "MiLK PLUS"),
    (19900, "FiNALE"),
    (20000, "DX"),
    (20500, "DX PLUS"),
    (21000, "Splash"),
    (21500, "Splash PLUS"),
    (22000, "UNiVERSE"),
    (22500, "UNiVERSE PLUS"),
    (23000, "FESTiVAL"),
    (23500, "FESTiVAL PLUS"),
    (24000, "BUDDiES"),
    (24500, "BUDDiES PLUS"),
    (25000, "PRiSM"),
    (25500, "PRiSM PLUS"),
    (26000, "CiRCLE"),
    (26500, "CiRCLE PLUS"),
    (27000, "MAGiCAL"),
    (27500, "MAGiCAL PLUS"),
)
JP_VERSION_NAMES = tuple(name for _, name in JP_VERSIONS)
