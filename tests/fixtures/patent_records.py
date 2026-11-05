"""Create invented REGPAT, PATSTAT and OPS records at public Manchester locations"""
from contextlib import closing
import csv
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from ingest.patent_metadata.resolve_ops_families import parse_application_dates, application_filing_date

LOCATIONS = (
    ("124 Oxford Road", "M13 9RD"),
    ("Stockport Road", "M12 6AL"),
    ("", "M13 9PL"),
)
APPLICATIONS = (
    ("90000000001", 0, "880000001"),
    ("90000000002", 0, "880000001"),
    ("90000000003", 1, "880000002"),
    ("90000000004", 2, "880000003"),
)
INVENTOR_LOCATIONS = {
    "880000001": (0, 1),
    "880000002": (1, 1, 2),
    "880000003": (2,),
}


def create(output: Path) -> dict:
    """Write the four source tables and both optional date/family sources"""
    output.mkdir(parents=True, exist_ok=False)
    release = output / "regpat"
    release.mkdir()
    rows = 0
    for office, code, role in (("EPO", "App", "applicant"), ("EPO", "Inv", "inventor"),
                              ("PCT", "App", "applicant"), ("PCT", "Inv", "inventor")):
        name, share = ("app_name", "app_share") if code == "App" else ("inv_name", "inv_share")
        fields = ["appln_id", "address", "city", "postal_code", "reg_code", "ctry_code", name, share]
        fields += ["person_id", "app_nbr", "pub_nbr"] if office == "EPO" else ["internat_appln_nr", "pct_nbr"]
        with (release / f"example_{office}_{code}_reg.txt").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter="|")
            writer.writeheader()
            # The PCT rows demonstrate date/family fallback and absent person IDs
            chosen = APPLICATIONS if office == "EPO" else APPLICATIONS[-1:]
            for aid, place, family in chosen:
                places = INVENTOR_LOCATIONS[family] if code == "Inv" else (place,)
                for index, location in enumerate(places, 1):
                    address, postcode = LOCATIONS[location]
                    row = dict(appln_id=aid, address=address, city="Manchester", postal_code=postcode,
                               reg_code="UKD33", ctry_code="GB",
                               **{name: f"Example {role} {index}", share: str(Decimal(1) / len(places))})
                    if office == "EPO":
                        row.update(person_id=f"example-{role}-{index}",
                                   app_nbr="EXAMPLE-EP-" + aid, pub_nbr=aid)
                    else:
                        row.update(internat_appln_nr="EXAMPLE-WO-" + aid, pct_nbr="202090000" + aid[-1])
                    writer.writerow(row)
                    rows += 1
    patstat = output / "patstat.sqlite"
    with closing(sqlite3.connect(patstat)) as db:
        db.execute("CREATE TABLE tls201_appln(appln_id TEXT PRIMARY KEY, appln_filing_year INTEGER, appln_filing_date TEXT, docdb_family_id TEXT, inpadoc_family_id TEXT)")
        db.executemany("INSERT INTO tls201_appln VALUES(?,?,?,?,?)",
                       [(aid, 2020, "2020-03-04" if aid != "90000000002" else "2020-06-01", family, family) for aid, _, family in APPLICATIONS[:3]])
        db.commit()
    ops = output / "ops.sqlite"
    xml_dir = output / "ops_xml"
    xml_dir.mkdir()
    with closing(sqlite3.connect(ops)) as db:
        db.execute("CREATE TABLE application(appln_id TEXT PRIMARY KEY, family_id TEXT, primary_reference TEXT)")
        db.execute("CREATE TABLE publication(reference TEXT PRIMARY KEY, status TEXT)")
        db.execute("CREATE TABLE application_date_evidence(reference TEXT,application_country TEXT,application_number TEXT,document_id_type TEXT,filing_date TEXT,record_id TEXT,application_reference_xml TEXT)")
        for aid, _, family in APPLICATIONS:
            for office in ("EP", "WO"):
                publication = aid if office == "EP" else "202090000" + aid[-1]
                number = "EXAMPLE-" + office + "-" + aid
                filing_date = "20200601" if aid == "90000000002" else "20200304"
                xml = (f'<root><exchange-document country="{office}" doc-number="{publication}">'
                       f'<application-reference><document-id document-id-type="docdb"><country>{office}</country>'
                       f'<doc-number>{number}</doc-number><date>{filing_date}</date></document-id>'
                       f'</application-reference></exchange-document></root>').encode()
                (xml_dir / f"{office}-{aid}.xml").write_bytes(xml)
                db.executemany("INSERT INTO application_date_evidence VALUES(?,?,?,?,?,?,?)", parse_application_dates(xml))
            db.execute("INSERT INTO application VALUES(?,?,?)", (aid, family, "EP." + aid))
            db.execute("INSERT INTO publication VALUES(?,?)", ("EP." + aid, "resolved"))
        db.commit()
    # A small fixture master exercises the same canonical-date intake as full runs
    import duckdb
    with closing(sqlite3.connect(ops)) as evidence, duckdb.connect() as writer:
        writer.execute('CREATE TABLE dates(appln_id VARCHAR, filing_date DATE, filing_year INTEGER, date_conflict BOOLEAN)')
        for aid, _, _ in APPLICATIONS:
            record = application_filing_date(evidence, references=['EP.' + aid],
                                            application_number='EXAMPLE-EP-' + aid, application_country='EP')
            assert record['status'] == 'valid'
            value = record['filing_date']
            writer.execute('INSERT INTO dates VALUES(?,?,?,false)',
                           [aid, f'{value[:4]}-{value[4:6]}-{value[6:]}', record['filing_year']])
        master = output / 'application-master.parquet'
        writer.execute('COPY dates TO ? (FORMAT PARQUET)', [str(master)])
    (output / 'release.json').write_text(json.dumps(dict(validated=True,
        release_id='synthetic-example-canonical-v1', data_kind='synthetic patent records'), indent=2) + '\n')
    manifest = dict(data_kind="synthetic patent records", source_rows=rows, distinct_addresses=len(LOCATIONS),
                    filing_year=2020, patent_sources=["REGPAT-format tables", "PATSTAT-format SQLite", "OPS-format cached XML and SQLite"],
                    geographic_inputs="Public Manchester address strings; geography and raster evidence supplied separately")
    (output / "patent_inputs.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
