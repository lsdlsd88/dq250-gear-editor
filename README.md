# DQ250 gear editor

Editor for the gear-ratio table inside VAG DQ250 (02E / 0D9) DSG flash dumps.
It finds the 21-byte tooth table, decodes the tooth counts and shows the mesh,
final drive and total ratio per gear live while you edit. Two sides, A and B,
hold two dumps at once, so an original and a modified file - or two different
files - can be compared and values copied between them. Saving always writes a
patched copy and never touches the source dump.

![DQ250 gear editor with two dumps loaded](docs/screenshot.png)
