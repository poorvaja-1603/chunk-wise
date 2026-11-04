"""Generate ChunkWise Project Logic and Architecture Word Document on Desktop."""
from __future__ import annotations

import os
from pathlib import Path
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn


def set_cell_background(cell, fill_hex: str) -> None:
    shading_elm = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    cell._tc.get_or_add_tcPr().append(shading_elm)


def format_table(table, col_widths, header_bg="0F5F66", row_alt_bg="F4F8F8") -> None:
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, row in enumerate(table.rows):
        for j, cell in enumerate(row.cells):
            cell.width = Inches(col_widths[j])
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            if i == 0:
                set_cell_background(cell, header_bg)
                for paragraph in cell.paragraphs:
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    for run in paragraph.runs:
                        run.font.bold = True
                        run.font.color.rgb = RGBColor(255, 255, 255)
                        run.font.size = Pt(9.5)
            else:
                if i % 2 == 0:
                    set_cell_background(cell, row_alt_bg)
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(9)


def create_document(output_path: Path) -> None:
    doc = Document()

    # Set page margins
    sections = doc.sections
    for section in sections:
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(0.8)
        section.right_margin = Inches(0.8)

    # Styles
    normal_style = doc.styles["Normal"]
    normal_style.font.name = "Calibri"
    normal_style.font.size = Pt(10.5)
    normal_style.font.color.rgb = RGBColor(40, 40, 40)

    # Title
    title = doc.add_paragraph()
    title_run = title.add_run("ChunkWise: System Logic & Architecture Specification")
    title_run.font.size = Pt(22)
    title_run.font.bold = True
    title_run.font.color.rgb = RGBColor(15, 95, 102)
    title.paragraph_format.space_after = Pt(2)

    subtitle = doc.add_paragraph()
    sub_run = subtitle.add_run("Content-Defined Chunk Storage Deduplication Engine with AWS S3 & DynamoDB")
    sub_run.font.size = Pt(13)
    sub_run.font.italic = True
    sub_run.font.color.rgb = RGBColor(90, 105, 120)
    subtitle.paragraph_format.space_after = Pt(16)

    # Metadata Banner
    banner = doc.add_paragraph()
    b_run = banner.add_run("Author: Poorvaja | Project: ChunkWise | Platform: Python 3.11, AWS S3, DynamoDB, CloudWatch | Status: Production")
    b_run.font.size = Pt(9.5)
    b_run.font.bold = True
    b_run.font.color.rgb = RGBColor(15, 95, 102)
    banner.paragraph_format.space_after = Pt(18)

    # Helper function for headings
    def add_heading_1(text: str):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(16)
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        run.font.size = Pt(15)
        run.font.bold = True
        run.font.color.rgb = RGBColor(15, 95, 102)
        return p

    def add_heading_2(text: str):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        run.font.size = Pt(12)
        run.font.bold = True
        run.font.color.rgb = RGBColor(40, 70, 90)
        return p

    def add_callout(text: str, title_text: str = "Key Principle"):
        table = doc.add_table(rows=1, cols=1)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        cell = table.cell(0, 0)
        cell.width = Inches(6.8)
        set_cell_background(cell, "EBF3F4")
        p = cell.paragraphs[0]
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after = Pt(4)
        t_run = p.add_run(f"{title_text}: ")
        t_run.font.bold = True
        t_run.font.size = Pt(9.5)
        t_run.font.color.rgb = RGBColor(15, 95, 102)
        c_run = p.add_run(text)
        c_run.font.size = Pt(9.5)
        doc.add_paragraph().paragraph_format.space_after = Pt(4)

    # 1. Executive Summary
    add_heading_1("1. Executive Summary & Problem Statement")
    doc.add_paragraph(
        "Modern cloud storage architectures face exponential cost growth due to duplicate data retention. "
        "Standard backup, document management, and log storage systems store massive redundant copies of files "
        "that share identical or near-identical content."
    )
    doc.add_paragraph(
        "Traditional deduplication approaches suffer from critical flaws:"
    )
    p = doc.add_paragraph()
    p.add_run("- Whole-File Hashing (Object-Level): ").bold = True
    p.add_run("Identifies duplicates only when an entire file matches 100%. Changing a single byte or character invalidates the entire hash, causing 0% deduplication savings.")
    p = doc.add_paragraph()
    p.add_run("- Fixed-Size Chunking: ").bold = True
    p.add_run("Divides files into rigid N-kilobyte blocks. It fails completely under insertion or deletion edits due to the boundary shift problem, where inserting one byte shifts every subsequent chunk boundary and triggers 100% cache misses.")

    add_callout(
        "ChunkWise solves this with Content-Defined Chunking (CDC). By computing chunk boundaries dynamically based on content rather than fixed offsets, insertions only invalidate the modified chunk, allowing all remaining chunks to resynchronize and deduplicate.",
        "The Solution"
    )

    # 2. Content-Defined Chunking Logic
    add_heading_1("2. Algorithmic Logic: Content-Defined Chunking (CDC)")
    doc.add_paragraph(
        "ChunkWise implements a Rabin-style polynomial rolling hash over a sliding window of bytes. "
        "The chunker reads a byte stream and maintains a running fingerprint in O(1) time per byte."
    )
    add_heading_2("2.1 Mathematical Foundations")
    doc.add_paragraph(
        "The rolling hash evaluates a polynomial over the Galois field defined by a large prime modulus:"
    )
    p = doc.add_paragraph()
    p.add_run("  - Prime Modulus: ").bold = True
    p.add_run("p = 2^61 - 1 (Mersenne prime, ensuring uniform hash distribution)")
    p = doc.add_paragraph()
    p.add_run("  - Base Multiplier: ").bold = True
    p.add_run("b = 257 (coprime with typical byte values)")
    p = doc.add_paragraph()
    p.add_run("  - Window Size: ").bold = True
    p.add_run("w = 48 bytes (optimal balance between locality and sensitivity)")

    doc.add_paragraph(
        "When sliding the window by one byte (dropping old byte c_out and consuming new byte c_in), the hash updates in O(1) operations:"
    )
    doc.add_paragraph("    H_new = ((H_old - c_out * b^(w-1)) * b + c_in) mod (2^61 - 1)")

    add_heading_2("2.2 Dynamic Boundary Identification & Sizing Bounds")
    doc.add_paragraph(
        "A chunk boundary is declared when the low-order bits of the rolling hash match a bitmask determined by the target average chunk size:"
    )
    doc.add_paragraph("    Boundary Condition: (fingerprint & (avg_size - 1)) == 0")
    doc.add_paragraph(
        "To avoid degenerate tiny chunks (excess metadata) and pathologically large chunks (poor deduplication), ChunkWise enforces strict hardware-tuned constraints:"
    )

    table_data = [
        ["Parameter", "Configured Value", "Algorithmic Purpose"],
        ["Minimum Chunk Size", "2,048 bytes (2 KiB)", "Suppresses boundary tests to eliminate tiny high-overhead chunks."],
        ["Average Target Size", "8,192 bytes (8 KiB)", "Target statistical average; mask = 0x1FFF (13 trailing zero bits)."],
        ["Maximum Chunk Size", "65,536 bytes (64 KiB)", "Forced cut-off boundary preventing unbounded memory allocation."],
        ["Sliding Window", "48 bytes", "Window of bytes evaluated for the rolling hash polynomial."],
        ["Chunk Identifier", "SHA-256 (64 hex chars)", "Cryptographically collision-resistant content digest."],
    ]
    t = doc.add_table(rows=len(table_data), cols=3)
    for i, row in enumerate(table_data):
        for j, val in enumerate(row):
            t.cell(i, j).text = val
    format_table(t, [1.8, 1.8, 3.2])

    # 3. Two-Phase Distributed Commit Architecture
    add_heading_1("3. Distributed Two-Phase Commit Logic & State Machines")
    doc.add_paragraph(
        "Because Amazon S3 (object store) and Amazon DynamoDB (metadata index) are independent distributed services without cross-service transactions, "
        "a naive uploader could crash mid-transfer, leaving ghost metadata or orphaned storage. "
        "ChunkWise enforces strict distributed invariants across both file and chunk lifecycles."
    )

    add_heading_2("3.1 File Record State Machine")
    doc.add_paragraph(
        "File registration follows a strict four-step write pipeline:"
    )
    p = doc.add_paragraph()
    p.add_run("1. Pending Marker: ").bold = True
    p.add_run("The file record is written to DynamoDB with status='pending'.")
    p = doc.add_paragraph()
    p.add_run("2. Manifest Persistence: ").bold = True
    p.add_run("Ordered manifest rows (FILE#<id>/MANIFEST#<sequence>) are committed in sequence.")
    p = doc.add_paragraph()
    p.add_run("3. Chunk References: ").bold = True
    p.add_run("Idempotent reference increments (REF#<file>/CHUNK#<hash>) update ref_count on each chunk.")
    p = doc.add_paragraph()
    p.add_run("4. Atomic Flip to Committed: ").bold = True
    p.add_run("Once all manifests and chunks are committed, the file record flips conditionally to status='committed'.")

    add_callout(
        "Read Isolation Rule: The read operations (get, verify, list_files, stats, and idempotency checks) strictly filter and query only committed files via the status-index GSI. If a writer crashes at any point before step 4, the uncommitted file is completely invisible to readers.",
        "Guaranteed Invariant I2"
    )

    add_heading_2("3.2 Chunk State Machine & Lease Claims")
    doc.add_paragraph(
        "Individual chunks transition through three lifecycle states: Pending, Committed, and Tombstone."
    )
    p = doc.add_paragraph()
    p.add_run("- Pending (Lease Claim): ").bold = True
    p.add_run("When a writer identifies a new chunk not present in the index, it issues a conditional claim recording its uploader ID and a 60-second lease TTL.")
    p = doc.add_paragraph()
    p.add_run("- Committed: ").bold = True
    p.add_run("Upon successful S3 PUT, the chunk transitions to status='committed'.")
    p = doc.add_paragraph()
    p.add_run("- Tombstone: ").bold = True
    p.add_run("When a file is deleted, chunk refcounts decrement. When refcount hits 0, the chunk is marked as status='tombstone' with a deletion timestamp.")

    # 4. AWS Request Efficiency
    add_heading_1("4. AWS Request Efficiency & Cost Optimization Logic")
    doc.add_paragraph(
        "In cloud storage, API request costs (S3 PUT/GET, DynamoDB Read/Write Units) can easily exceed raw storage pricing. "
        "ChunkWise implements multiple optimization layers:"
    )

    p = doc.add_paragraph()
    p.add_run("1. Index-First Batch Deduplication: ").bold = True
    p.add_run("Before issuing any S3 requests, ChunkWise queries DynamoDB using BatchGetItem (up to 100 chunk hashes per round-trip). Chunks already committed bypass S3 completely, cutting S3 PUT API costs to near zero for duplicate uploads.")

    p = doc.add_paragraph()
    p.add_run("2. In-File Duplicate Filtering: ").bold = True
    p.add_run("An in-memory seen-hash set filters repetitive patterns inside the same file (e.g. padded zeros or repeated strings) before any cloud lookup.")

    p = doc.add_paragraph()
    p.add_run("3. S3 Conditional PUT with 412 Handling: ").bold = True
    p.add_run("New chunk uploads include the header If-None-Match: *. If another concurrent uploader wrote the same chunk a millisecond earlier, S3 returns HTTP 412 (Precondition Failed). ChunkWise catches 412 and treats the chunk as successfully deduplicated without failure.")

    p = doc.add_paragraph()
    p.add_run("4. O(1) Atomic Accounting Counters: ").bold = True
    p.add_run("Rather than running an expensive full-table DynamoDB Scan to compute storage totals, ChunkWise maintains an atomic counters item (pk='COUNTERS', sk='TOTALS'). Updates use DynamoDB ADD expressions, returning totals in O(1) time.")

    p = doc.add_paragraph()
    p.add_run("5. S3 Prefix Sharding: ").bold = True
    p.add_run("Chunk objects are stored under objects/<hh>/<rest> (e.g. objects/d0/4de10fdca...). The two-character hex prefix shards writes across 256 distinct S3 partition prefixes, preventing S3 throttle limits.")

    # 5. Recovery Subsystem
    add_heading_1("5. Self-Healing, Recovery, and Storage Lifecycle")
    doc.add_paragraph(
        "Storage consistency is verified through self-healing command utilities:"
    )

    p = doc.add_paragraph()
    p.add_run("A. chunkwise fsck (File System Consistency Check): ").bold = True
    p.add_run("Audits the entire storage system. It scans all committed file manifests, recomputes actual reference counts from scratch, detects drift, repairs corrupted refcounts, discovers orphan S3 objects not indexed, and identifies stale pending files older than max-pending-age.")

    p = doc.add_paragraph()
    p.add_run("B. chunkwise delete FILE_ID: ").bold = True
    p.add_run("Deletes the file record and atomically decrements refcount across all referenced chunks. Chunks reaching refcount 0 transition to tombstone status.")

    p = doc.add_paragraph()
    p.add_run("C. chunkwise gc --grace-seconds N: ").bold = True
    p.add_run("Garbage collection sweeps tombstoned chunks older than the configured grace period (default 24 hours). Before issuing physical S3 deletions, it re-verifies that refcount is still 0.")

    # 6. Empirical Benchmarks
    add_heading_1("6. Empirical Benchmark Results")
    doc.add_paragraph(
        "Full benchmark measurements (bench/run_bench.py) on standardized datasets:"
    )

    bench_table = [
        ["Dataset", "Chunker", "Files", "Logical Size", "Physical Size", "Dedup Ratio", "Storage Saved", "Put Time"],
        ["D1_random", "CDC", "1", "16.00 MiB", "16.00 MiB", "1.0000", "0.00%", "22.96 s"],
        ["D1_random", "Fixed", "1", "16.00 MiB", "16.00 MiB", "1.0000", "0.00%", "10.82 s"],
        ["D2_duplicates", "CDC", "4", "32.00 MiB", "8.00 MiB", "4.0000", "75.00%", "30.05 s"],
        ["D2_duplicates", "Fixed", "4", "32.00 MiB", "8.00 MiB", "4.0000", "75.00%", "4.50 s"],
        ["D3_edited_text", "CDC", "2", "16.00 MiB", "8.01 MiB", "1.9974", "49.93%", "16.08 s"],
        ["D3_edited_text", "Fixed", "2", "16.00 MiB", "12.00 MiB", "1.3333", "25.00%", "6.02 s"],
        ["D4_edited_binary", "CDC", "2", "16.00 MiB", "8.02 MiB", "1.9961", "49.90%", "16.79 s"],
        ["D4_edited_binary", "Fixed", "2", "16.00 MiB", "14.00 MiB", "1.1428", "12.50%", "7.22 s"],
        ["D5_small_files", "CDC", "4000", "15.62 MiB", "7.81 MiB", "2.0000", "50.00%", "36.93 s"],
        ["D5_small_files", "Fixed", "4000", "15.62 MiB", "7.81 MiB", "2.0000", "50.00%", "24.97 s"],
    ]
    tb = doc.add_table(rows=len(bench_table), cols=8)
    for i, row in enumerate(bench_table):
        for j, val in enumerate(row):
            tb.cell(i, j).text = val
    format_table(tb, [1.2, 0.6, 0.5, 0.9, 0.9, 0.8, 0.9, 0.8])

    doc.add_paragraph().paragraph_format.space_before = Pt(6)
    add_callout(
        "Key Quantitative Takeaway: On edited text and binary data (D3 and D4), Rabin CDC achieves 49.9% storage savings compared to only 12.5% to 25.0% for fixed-size chunking, proving resilience against the boundary shift problem.",
        "Experimental Finding"
    )

    # 7. Viva Voce Cheat Sheet
    add_heading_1("7. Viva Voce & Presentation Defense Guide")
    doc.add_paragraph(
        "Common questions professors ask and recommended technical answers:"
    )

    qa_list = [
        ("Q: Why use a rolling hash instead of computing SHA-256 over fixed blocks?",
         "A: Fixed blocks fail under edits. If one byte is added at the start of a 100 MB file, every subsequent 8 KB block boundary shifts by 1 byte, making all hashes change (0% deduplication). Rabin rolling hash evaluates a sliding window in O(1) time and breaks chunks when content matches a pattern, so an edit only affects that single chunk while all downstream chunks stay identical."),
        
        ("Q: How does the system prevent corrupted data from being returned on download?",
         "A: Every chunk is verified against its SHA-256 hash when fetched from S3. Once all chunks are assembled, the engine verifies the whole-file SHA-256 against the metadata record. If any bit fails verification, it immediately raises IntegrityError and aborts without outputting corrupted bytes."),

        ("Q: How do you prevent race conditions when two writers upload the same chunk simultaneously?",
         "A: First, writers acquire a short-lived lease in DynamoDB. Second, uploads to S3 use conditional headers (If-None-Match: *). If two writers race, the second writer receives an HTTP 412 Precondition Failed from S3, which ChunkWise catches and treats as a successful deduplication."),

        ("Q: What is the trade-off with chunk sizes (4 KiB vs 8 KiB vs 16 KiB)?",
         "A: Smaller chunks give slightly higher deduplication ratios, but double the number of chunks and metadata overhead. In our sweep benchmark, moving from 4 KiB to 8 KiB halved the metadata overhead (5.26% down to 2.63%) with virtually identical deduplication (1.9977 vs 1.9974). 8 KiB is the Pareto-optimal chunk size."),

        ("Q: What happens if an uploader crashes mid-transfer?",
         "A: Files are written with status='pending'. The file record is flipped to status='committed' only as the very last step. Reads only query committed files via the status-index GSI. The background chunkwise fsck command identifies any abandoned pending files and cleans up orphaned chunks safely."),
    ]

    for q, a in qa_list:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.keep_with_next = True
        q_run = p.add_run(q)
        q_run.bold = True
        q_run.font.color.rgb = RGBColor(15, 95, 102)
        p_ans = doc.add_paragraph()
        p_ans.paragraph_format.space_after = Pt(6)
        a_run = p_ans.add_run(a)
        a_run.font.size = Pt(10)

    # Save document
    doc.save(str(output_path))
    print(f"Document successfully created at: {output_path}")


if __name__ == "__main__":
    desktop_target = Path(r"C:\Users\Poorvaja\Desktop\ChunkWise_Project_Logic_and_Architecture.docx")
    create_document(desktop_target)
