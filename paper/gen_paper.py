#!/usr/bin/env python3
"""Generate main.tex for the symbolic dynamics paper. Run: python gen_paper.py"""
import os

OUT = os.path.join(os.path.dirname(__file__), 'main.tex')

doc = []

doc.append(r"""\documentclass{article}

\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{booktabs}
\usepackage{hyperref}
\usepackage{enumitem}
\hypersetup{colorlinks=true,urlcolor=blue,citecolor=blue}

\title{Monitoring LLM Agent Behavior with Symbolic Dynamics:\\
A System Description and Experience Report}

\author{Cognitive Workshop}
\date{June 2026}

\begin{document}
\maketitle

\begin{abstract}
This is a system description and experience report. We are not LLM researchers; we are engineers who built an agent harness and observed that existing monitoring tools only check \emph{what the model says}, not \emph{what the agent does}. We experimented with applying symbolic dynamics---an old idea from dynamical systems theory---to monitor agent tool-call sequences in real time. The approach maps tool calls and hook verdicts to a discrete symbol stream, computes Shannon entropy and a transition-graph complexity metric over rolling windows, and checks for forbidden symbol sequences under a subshift-of-finite-type constraint. It runs in approximately 200ms, requires no model internals (works with API-only LLMs), and costs nothing per check. We deployed it in a single-agent harness over 30 days (104 sessions) and observed three cases where it caught behavioral anomalies that semantic verification missed. We do \emph{not} claim this outperforms existing methods; we have no quantitative benchmarks, no ablation studies, and no comparison against baselines. We are sharing the architecture, the math (with its known approximations), and the code in case others find the approach useful or want to improve on it.
\end{abstract}
""")

doc.append(r"""
\section{Introduction}

\paragraph{Who we are.}
We are not LLM researchers. We built an agent harness---a set of lifecycle hooks, state files, and safety gates that wrap around a Claude Code session---to help with long-running engineering tasks (CAD design, data analysis, document generation). After roughly one month of daily use across 104 sessions, we noticed that our semantic verification gate (a separate LLM auditing outputs) caught factual errors but systematically missed a different class of problem: the agent getting stuck in behavioral loops, or its tool-use patterns drifting into territory we had never seen before.

\paragraph{The gap we observed.}
Existing monitoring methods focus on \emph{content}---detecting hallucinations in generated text. Semantic entropy~\cite{farquhar2024detecting}, hidden-state trajectory analysis~\cite{mir2025geometry}, and adaptive sampling~\cite{xjdr2025entropix} all answer ``is this output correct?'' They do not answer ``is the agent behaving normally?'' An agent can produce perfectly correct individual outputs while trapped in a loop, or while its action distribution slowly shifts into an unsafe regime. Content-based detectors are blind to these patterns by design.

\paragraph{What we tried.}
We experimented with \textbf{symbolic dynamics}~\cite{lind2021introduction}: an old mathematical framework for analyzing discrete symbol sequences. The idea is simple---treat the agent's tool calls as a stream of symbols, and monitor the stream's statistical properties in real time. When the Shannon entropy collapses, the agent is probably stuck. When the transition graph acquires too many new edges, the agent is probably exploring unsafely. When a known-bad symbol sequence appears, something is definitely wrong.

\paragraph{What this paper is (and is not).}
This is a system description and experience report. We describe the architecture we built, the math we used (with its known approximations noted explicitly), and three qualitative cases from our deployment. We do \emph{not} claim superior performance over any baseline. We have no quantitative benchmarks, no ablation studies, and no comparison against existing methods. We open-sourced the code and are sharing what we learned, in case others find the approach useful.

Our design goals for the monitoring layer were:
\begin{enumerate}[leftmargin=*,itemsep=2pt]
\item \textbf{Behavioral, not semantic.} Monitor tool-call patterns and hook verdicts---externally observable events.
\item \textbf{Black-box compatible.} No model internals required. Works with any API-only LLM.
\item \textbf{Real-time and zero-cost.} Approximately 200ms per check, pure Python computation, no extra API calls.
\item \textbf{Simple enough to debug.} If the monitor itself is a black box, we have just moved the trust problem elsewhere.
\end{enumerate}
""")

doc.append(r"""
\section{Related Work}

\paragraph{Semantic Entropy for Hallucination Detection.}
Farquhar et al.~\cite{farquhar2024detecting} introduced semantic entropy---clustering LLM outputs by bidirectional entailment and computing entropy over meaning classes---as an unsupervised hallucination detector (AUROC $\sim$0.79 across 30 model--task combinations). Subsequent work improved sample efficiency via Bayesian estimation~\cite{ciosek2025hallucination} and alphabetic hybrid estimation~\cite{pan2026mind}. These methods require 5--20 model samples per detection and operate on output text, not agent behavior.

\paragraph{Geometric and Spectral Approaches.}
LSD~\cite{mir2025geometry} analyzes hidden-state trajectories across transformer layers, using cosine similarity and L2 velocity to detect hallucinations (AUROC 0.96 on TruthfulQA, single forward pass). Graph signal processing~\cite{noel2025graph} computes spectral entropy over attention graphs (88.75\% accuracy). Both require full access to model internals---inapplicable to API-only deployments.

\paragraph{Agent Behavior Analysis.}
The closest prior work to ours is Deng~\cite{deng2026agentgenome}, which encodes ReAct agent traces into a 4-letter alphabet (Explore, Execute, Plan, Verify) and applies n-gram mining with Markov transition analysis. This demonstrates that behavioral patterns contain predictive signal for task success (trigram P-X-P lowers success rate by 10.4\%), but operates post-hoc with a small fixed alphabet and does not incorporate topological invariants. ProbGuard~\cite{chen2025probguard} learns Discrete-Time Markov Chains from execution traces (predicting violations up to 38.66s in advance), providing PAC guarantees on learned models, but focuses on predicting known unsafe states rather than detecting novel anomalies.

\paragraph{Our Positioning.}
We do not claim to advance the state of the art. The closest prior work (Deng 2026, encoding ReAct traces as 4-letter symbols with n-gram mining) independently converged on the same core insight---that agent behavior has a statistical signature worth monitoring---and did it more rigorously. Our contribution is narrower: we combined three signals (frequency entropy, transition complexity, forbidden words) into a real-time, black-box-compatible pipeline, and we ran it in production for a month. The SFT formalism was useful as a design tool (``what should the legal behavior space look like?''), but we acknowledge the gap between the infinite-sequence theory and our 100-observation sliding window implementation.
""")

doc.append(r"""
\section{Method}

\subsection{Event-to-Symbol Mapping}

Let the agent's observable behavior be a sequence of discrete events $e_1, e_2, e_3, \ldots$ where each $e_t$ is a tuple $(\text{tool\_name}, \text{hook\_verdict}, \text{domain})$.
We define a finite alphabet $\mathcal{A} = \{s_1, s_2, \ldots, s_n\}$ and a deterministic mapping $\phi: \mathcal{E} \to \mathcal{A}$ that assigns each event to a symbol.

The mapping is domain-specific: different monitoring domains define different alphabets. For example, the \texttt{hook} domain uses an alphabet of 10 symbols encoding PreToolUse check outcomes (allow, ask, deny patterns), while the \texttt{dialogue} domain uses a separate alphabet for conversation patterns. This yields $K$ parallel observation streams $\omega^{(1)}, \omega^{(2)}, \ldots, \omega^{(K)}$, one per domain.

\subsection{Shannon Entropy (Symbol Frequency)}

Over a rolling window of $N$ observations in domain $k$, we compute the empirical symbol probabilities $p^{(k)}(s_i) = \text{count}(s_i) / N$. The Shannon entropy is:

\begin{equation}
H^{(k)} = -\sum_{i=1}^{n_k} p^{(k)}(s_i) \log_2 p^{(k)}(s_i)
\end{equation}

\textbf{Interpretation.} $H^{(k)} \approx 0$ indicates the agent is repeating a single symbol---stuck in a loop. $H^{(k)} \approx \log_2(n_k)$ indicates maximum entropy---the agent is exploring broadly or behaving erratically. Sudden jumps in $H^{(k)}$ signal phase transitions where the behavioral regime changes abruptly. A healthy system shows moderate entropy fluctuating within a calibrated baseline.

\subsection{Topological Entropy (Transition Structure)}

Shannon entropy treats the symbol sequence as independent draws, discarding transition information. Real anomalies often manifest in \emph{transitions}---the agent begins generating symbol sequences never observed during normal operation.

We construct an adjacency matrix $T^{(k)} \in \{0,1\}^{n_k \times n_k}$ over the rolling window, where $T^{(k)}_{ij} = 1$ if symbol $s_j$ has ever followed symbol $s_i$ in the observation window. This defines a directed transition graph over $\mathcal{A}^{(k)}$.

The \textbf{spectral radius} $\rho(T^{(k)})$---the largest eigenvalue of $T^{(k)}$---bounds the growth rate of distinct symbol paths:

\begin{equation}
h_{\text{top}}^{(k)} \approx \log_2 \rho(T^{(k)})
\end{equation}

\textbf{Important caveat.} The formula $h_{\text{top}} \approx \log_2 \rho(T)$ is an approximation. Strictly, $h_{\text{top}} = \log \lambda_{\text{max}}$ holds for irreducible non-negative integer transition matrices of Markov SFTs; our $T$ is a 0-1 adjacency matrix over a sliding window. The spectral radius of $T$ is better understood as a \textbf{transition complexity index}---an upper bound on topological entropy rather than its exact value. We use it as a practical complexity metric, not as a rigorous dynamical invariant. Its behavior (monotonic growth under exploration, stability under routine operation) is empirically useful in our deployment, but we have no formal proof of its relationship to true topological entropy in this setting.

\subsection{Forbidden Words (Subshift of Finite Type)}

A \textbf{forbidden word} is a finite symbol sequence that must never appear in the observation stream. For example, three consecutive \texttt{DENY} verdicts in the \texttt{hook} domain signals an attack pattern or configuration error.

The set of all infinite symbol sequences over $\mathcal{A}$ that avoid a finite set $\mathcal{F}$ of forbidden words defines a \textbf{subshift of finite type} (SFT):

\begin{equation}
X_{\mathcal{F}} = \{ \omega \in \mathcal{A}^{\mathbb{N}} : \text{no } w \in \mathcal{F} \text{ appears as a subword of } \omega \}
\end{equation}

$X_{\mathcal{F}}$ is the \textbf{legal behavior space} for that domain. Any observed sequence falling outside $X_{\mathcal{F}}$ triggers a forbidden-word alert. Forbidden words are classified into two severity levels: P0 (block---active attack, configuration error, system compromise) and P1 (warn---statistically unusual sequence, warrants attention).

A zero-observation penalty applies to safety-critical domains: if the \texttt{hook} domain receives zero observations in a monitoring window, the domain is treated as effectively unmonitored and marked \texttt{critical}.

\subsection{Stability Score}

The three signals are combined into a single stability score:

\begin{equation}
S^{(k)} = w_1 \cdot f_1(H^{(k)}, H_{\text{baseline}}) + w_2 \cdot f_2(\rho(T^{(k)}), \rho_{\text{baseline}}) + w_3 \cdot f_3(|\mathcal{F}_{\text{hit}}|)
\end{equation}

where $f_1, f_2$ are deviation functions measuring distance from calibrated baselines ($H_{\text{baseline}}, \rho_{\text{baseline}}$) and $f_3$ is a step function keyed to the maximum severity of any forbidden-word hit in the window. The score maps to four states:

\begin{center}
\begin{tabular}{lll}
\toprule
$S^{(k)}$ Range & State & Action \\
\midrule
$\geq 0.9$ & \texttt{ok} & Normal operation \\
$\geq 0.7$ & \texttt{warn} & Elevated, monitor \\
$\geq 0.5$ & \texttt{critical} & Active intervention needed \\
$< 0.5$ & \texttt{diverging} & Emergency response \\
\bottomrule
\end{tabular}
\end{center}

All metrics use rolling windows (default $N=100$ observations). As the window advances, old observations age out, so temporary anomalies decay naturally without manual reset.
""")

doc.append(r"""
\section{Architecture}

\subsection{Deployment Pipeline}

The system is deployed as a multi-domain pipeline integrated into the agent harness lifecycle:

\begin{enumerate}[leftmargin=*,itemsep=1pt]
\item \textbf{Observer} ($\sim$10ms): Maps each tool call and hook verdict to domain symbols, appends to per-domain JSONL logs.
\item \textbf{Engine} ($\sim$150ms): 8 domain engines independently compute entropy, spectral radius, and forbidden-word counts over rolling windows. Pure Python standard library, no API calls.
\item \textbf{Verdict} ($\sim$20ms): Aggregated health states written to a shared verdict file (\texttt{symbolic\_verdict.json}).
\item \textbf{Gate} ($<$5ms): PreToolUse lifecycle hook reads current verdict before every tool call. \texttt{critical} or \texttt{diverging} state triggers DENY; \texttt{warn} triggers ASK.
\end{enumerate}

Total end-to-end latency: $\sim$200ms. Zero additional API cost.

\subsection{Domain Coverage}

\begin{center}
\begin{tabular}{lcl}
\toprule
Domain & Alphabet Size & Sensitivity \\
\midrule
\texttt{hook} (safety gate outcomes) & 10 & High \\
\texttt{dialogue} (conversation patterns) & 10 & Medium \\
\texttt{cad} (CAD modeling) & 9 & Medium \\
\texttt{quant} (computation) & 8 & Medium \\
\texttt{image} (image processing) & 8 & Medium \\
\texttt{window} (cross-session) & Variable & Low \\
\texttt{pic} (plasma simulation) & Variable & Low \\
\texttt{retrieval} (knowledge retrieval) & Variable & Low \\
\bottomrule
\end{tabular}
\end{center}

Each domain is independently monitored. An anomaly in one domain (e.g., \texttt{cad}) does not affect the verdict for other domains (e.g., \texttt{dialogue}), preventing cascading false alarms across unrelated subsystems.

\subsection{Orthogonality to Semantic Verification}

The symbolic dynamics gate (CHECK 9) operates alongside a dual-AI semantic verification gate (CHECK 16) in the same PreToolUse hook. These are orthogonal signals:

\begin{itemize}[leftmargin=*,itemsep=1pt]
\item \textbf{Symbolic dynamics}: Fast ($\sim$200ms), statistical, behavioral. Detects loops, phase transitions, and forbidden patterns. Zero API cost.
\item \textbf{Dual-AI gate}: Slower ($\sim$3--5s), semantic, content-based. Verifies factual correctness via independent model audit. Requires API call.
\end{itemize}

The two layers catch failures that the other misses: a semantic audit cannot detect a behavioral loop, and a symbolic dynamics check cannot detect a factual error in a single output.
""")

doc.append(r"""
\section{Case Studies (Illustrative Only)}

The architecture has been deployed in a single-agent harness over 104 sessions spanning 30 days (May--June 2026). \textbf{These are qualitative examples, not quantitative evaluation.} We selected three cases that illustrate the kinds of anomaly each metric can detect. We do not report precision, recall, F1, or AUROC because we have no labeled test set and no baseline comparison. The examples below are meant to show \emph{what the system can see}, not \emph{how well it performs}.

\paragraph{Case 1: Entropy Collapse.}
During a multi-hour CAD design task, the \texttt{cad} domain's Shannon entropy dropped from a baseline of $H \approx 1.8$ to $H \approx 0.3$ over 40 tool calls. Investigation revealed the agent repeatedly calling the same export function with minor parameter variations---a behavioral loop where each command was individually valid. The stability score transitioned from \texttt{ok} to \texttt{warn}. We manually intervened and redirected the agent.

\paragraph{Case 2: Transition Graph Expansion.}
During a retrieval task, the transition graph's spectral radius $\rho(T)$ rose from a baseline of approximately 1.2 to approximately 3.8 over 60 observations, acquiring 14 edges absent from the calibration baseline. The agent had begun issuing novel search-fetch combinations. Shannon entropy remained within its normal range ($H \approx 1.6 \pm 0.2$), and no forbidden words were triggered---we would have missed this without the topological metric. We cannot confirm whether this was genuinely dangerous divergence or harmless exploration; the system flagged it, and we investigated.

\paragraph{Case 3: Forbidden-Word Intercept.}
Three consecutive \texttt{DENY} verdicts (a P0 forbidden word in the \texttt{hook} domain) occurred during a session where repeated tool failures triggered the compute gate. The monitor blocked further write operations before the agent compounded the error. Post-hoc review confirmed all individual tool outputs were valid---the escalating failure was only visible in the behavioral pattern.

\paragraph{Caveats.}
These cases were cherry-picked post-hoc to match the three metrics. We do not know the false-positive rate in normal operation. We have not measured whether a simple rule (``alert if the same tool is called N times consecutively'') would catch Case 1 just as well. This section exists to show the system produced non-trivial signals, not to prove it produces \emph{good} signals.
""")

doc.append(r"""
\section{Discussion and Limitations}

\paragraph{Limitations.}
(1) Detection quality depends on alphabet granularity: too coarse an alphabet loses discriminative power; too fine produces sparse observations with unreliable statistics. Domain-specific tuning of alphabet size is currently manual. (2) Baseline calibration ($H_{\text{baseline}}, \rho_{\text{baseline}}$) requires a representative sample of normal behavior for each domain; domain shift over time may require recalibration. (3) Forbidden words are manually defined based on operator experience; automatic discovery from historical anomaly logs is a direction for future work. (4) The current evaluation is based on a single deployment (one agent harness, one operator); cross-system validation on public agent benchmark trajectories (e.g., SWE-bench~\cite{yang2024sweagent}) is needed to establish generality.

\paragraph{Relationship to Content-Based Methods.}
Symbolic dynamics monitoring is not a replacement for semantic verification---it is an orthogonal layer that detects a class of anomalies (behavioral loops, phase transitions, forbidden action patterns) that content-based methods are structurally blind to. The combination of fast statistical behavioral monitoring with slower semantic content verification provides defense-in-depth for production agent deployments.

\paragraph{Future Work.}
Beyond automatic forbidden-word mining, several directions are promising: (1) cross-system validation on public agent trace datasets (SWE-bench, AgentBench) to establish quantitative benchmarks; (2) theoretical analysis of the relationship between alphabet granularity $n_k$ and detection power as a function of observation window size $N$; (3) integration with DTMC-based approaches~\cite{chen2025probguard} for joint statistical guarantees combining transition structure with probabilistic safety bounds; (4) extending the SFT framework to handle stochastic forbidden words with graded severity based on observed frequency rather than binary detection.
""")

doc.append(r"""
\section*{Acknowledgments}
We thank the open-source LLM agent community for discussions that motivated this work. The described system, including the full 8-domain symbolic dynamics pipeline, 7-hook lifecycle architecture, and a runnable demonstration, is available as open-source software at \url{https://github.com/zyc1419456542/cls-cognitive-loop}.

\bibliographystyle{plain}
\begin{thebibliography}{99}

\bibitem{farquhar2024detecting}
S.~Farquhar, J.~Kossen, L.~Kuhn, and Y.~Gal.
\newblock Detecting hallucinations in large language models using semantic entropy.
\newblock \emph{Nature}, 630:625--630, 2024.

\bibitem{mir2025geometry}
A.~Mir.
\newblock The geometry of truth: Layer-wise semantic dynamics for hallucination detection.
\newblock arXiv:2510.04933, 2025.

\bibitem{noel2025graph}
P.~No{\"e}l et al.
\newblock A graph signal processing framework for hallucination detection in LLMs.
\newblock arXiv:2510.19117, 2025.

\bibitem{ciosek2025hallucination}
K.~Ciosek et al.
\newblock Hallucination detection on a budget: Efficient {Bayesian} estimation of semantic entropy.
\newblock arXiv:2504.03579, 2025.

\bibitem{pan2026mind}
L.~Pan et al.
\newblock Mind the unseen mass: Unmasking {LLM} hallucinations via soft-hybrid alphabet estimation.
\newblock arXiv:2604.19162, 2026.

\bibitem{xjdr2025entropix}
xjdr-alt.
\newblock {Entropix}: Entropy based sampling and parallel {CoT} decoding.
\newblock \url{https://github.com/xjdr-alt/entropix}, 2025.

\bibitem{deng2026agentgenome}
S.~Deng.
\newblock Your agent has a genome: Sequence-level behavioral analysis and runtime governance of {LLM}-powered autonomous agents.
\newblock arXiv:2606.15579, 2026.

\bibitem{chen2025probguard}
Y.~Chen et al.
\newblock {ProbGuard}: Proactive safety monitoring for {LLM} agents via probabilistic model checking.
\newblock arXiv:2508.00500, 2025.

\bibitem{lind2021introduction}
D.~Lind and B.~Marcus.
\newblock \emph{An Introduction to Symbolic Dynamics and Coding}, 2nd ed.
\newblock Cambridge University Press, 2021.

\bibitem{yang2024sweagent}
J.~Yang et al.
\newblock {SWE-agent}: Agent-computer interfaces enable automated software engineering.
\newblock arXiv:2405.15793, 2024.

\bibitem{boiko2023emergent}
D.~Boiko et al.
\newblock Emergent autonomous scientific research capabilities of large language models.
\newblock arXiv:2304.05332, 2023.

\bibitem{deng2023mind2web}
X.~Deng et al.
\newblock {Mind2Web}: Towards a generalist agent for the web.
\newblock arXiv:2306.06070, 2023.

\end{thebibliography}

\end{document}
""")

# Write
with open(OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(doc))

print(f'main.tex written: {os.path.getsize(OUT):,} bytes')
