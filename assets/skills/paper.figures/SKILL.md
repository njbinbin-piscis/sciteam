# SKILL: paper.figures

**Purpose**: specify figures/tables whose data is traceable to frozen results.

**Inputs**: official results files.

**Outputs**: figure/table specs: data source (result id), axes, message.

**Steps**
1. One message per figure; state it as a caption-ready sentence.
2. Data column = result ids, never hand-copied numbers.
3. Baselines appear in every comparative figure.

**Failure modes**: truncated axes that exaggerate deltas; figures with data
that has no result id.

**Stop**: each spec names source ids + message.
