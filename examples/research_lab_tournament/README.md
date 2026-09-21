# Autonomous Research Lab Hypothesis Tournament

A reference production deployment of [SciTeam](https://github.com/njbinbin-piscis/sciteam)
demonstrating **Institution Engineering** applied to autonomous scientific research,
hypothesis generation, and blind peer-review tournaments.

## The Epistemic Problem in AI Co-Scientists

Unconstrained multi-agent research tools often suffer from:
* **Hallucinatory Agreement**: Multiple model instances enthusiastically agree on flawed or physically impossible hypotheses without adversarial friction.
* **Non-Falsifiable Claims**: Agents formulate vague predictions that cannot be refuted by experiment.
* **Plagiarism & Redundancy**: LLM generators reproduce training data without novelty certification.

## The Institutional Architecture

This reference deployment establishes a strict scientific institution:
1. **Hypothesis Generation (`generation`)**: Formulates novel, testable claims with concrete falsification criteria.
2. **Skeptical Critic (`reflection`)**: Examines physical plausibility and falsifiability. Holds explicit `may_veto` standing to disqualify unscientific or contradictory claims.
3. **Blind Tournament Judge (`ranking`)**: Ranks surviving candidates via pairwise ELO evaluation without author identity bias.
4. **Literature Proximity Auditor (`proximity`)**: Audits cosine distance against PubMed/arXiv literature to guarantee genuine scientific novelty.
5. **Consensus Meta-Reviewer (`meta_review`)**: The sole post empowered to synthesize the final certified research agenda into an exit artifact.

## Running the Tournament

```bash
# Run the tournament
python examples/research_lab_tournament/run.py --out ./runs/research_tournament_demo

# View the interactive HTML trajectory
open runs/research_tournament_demo/tournament_trajectory.html
```

## Running the Tests

```bash
pytest examples/research_lab_tournament/test_tournament.py
```
