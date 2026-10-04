# Market research: similar gateways and paid resellers (2026-10-04)

Web search snapshot. Prices come from the sellers' pages and third-party reviews; several sources
are 6–13 months old, so re-check before acting on them. Conversions: ¥7.1 = $1, €1 = $1.15.

## Similar self-hosted gateways (free, not resellers)

Claude Code proxies with per-user keys, limits and a dashboard:
[devasheeshG/claude-code-proxy](https://github.com/devasheeshG/claude-code-proxy),
[asyncdargen/claude-proxy](https://github.com/asyncdargen/claude-proxy),
[Ken-Chy129/llm-proxy](https://github.com/Ken-Chy129/llm-proxy),
[jaynlabs/jaynshare](https://github.com/jaynlabs/jaynshare),
[prathamVaidya/claude-share](https://github.com/prathamVaidya/claude-share),
[jkugsiya/DeviceGate](https://github.com/jkugsiya/DeviceGate) (one person's own machines).
Most pool several accounts with failover, which this project does not.

Pooling several accounts for one user:
[teamclaude](https://github.com/KarpelesLab/teamclaude),
[maxpool](https://github.com/2solarmax/maxpool),
[p4u/claude-proxy](https://github.com/p4u/claude-proxy).

API-key gateways: [LiteLLM](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)
(Anthropic's documented option), Portkey, Bifrost,
[claude-code-aws-gateway](https://github.com/antkawam/claude-code-aws-gateway) (Bedrock).
More under the [claude-code-proxy topic](https://github.com/topics/claude-code-proxy).

## Paid resellers

| Service | Model | Plan | Price |
|---|---|---|---|
| [G2G](https://www.g2g.com/categories/claude-accounts) | shared login | shared Pro / shared Max / private Max | ~$3.99 / $34.99 / $152 |
| [GamsGo](https://www.gamsgo.com/accounts/claude) | shared login | shared Max 5x, 10 days | $80 |
| [CheapClaude.online](https://cheapclaude.online/) | shared login, crypto via Telegram | Pro / Max 5x / Max 20x per 30 days | €4.95 / €18.95 / €38.95 |
| [Easy Claude Code](https://easyclaude.com/en) | relay, subscription carpool | per person/month: solo / 2 / 3 / 5 | ¥2199 / ¥1149 / ¥949 / ¥499 |
| AIGoCode | relay | 4 weeks, ¥110 credit/week | ¥399 |
| [RelayAPI](https://www.relayapi.org/) | relay | month / year | $10 / $80 |
| GAC Code | relay (listing >1 year old) | Lite / Claude Code / Max per month | ¥169 / ¥299 / ¥599 |
| AICoding | relay over pooled Max 20x | pay-as-you-go | 1.9× multiplier |

Relays bill credit at ¥0.85–1.5 per $1 on genuine Max/API channels, ¥0.1–0.3 on
reverse-engineered ones. Comparisons: [relay review](https://github.com/LMU-AI/claude-api-relay-review),
[buying guide](https://www.helpaio.com/guides/claude-code-relay), [directory](https://proxycc.cc/en/).
Many relays run [sub2api](https://github.com/Wei-Shaw/sub2api).

Anthropic direct: Pro $20, Max 5x $100, Max 20x $200 per month.

## Compared with our tickets

Normalised to dollars per 1% of one Max 20x account per month (resellers assumed ~5 buyers per
account):

| Option | Per month | Share | $ / 1% |
|---|---|---|---|
| Our Lite / Standard (`deploy/lightsail/config.toml`) | $20 / $100 | 5% / 25% | 4.00 |
| Easy Claude Code, 5 sharing | ~$70 | ~20% | 3.50 |
| Easy Claude Code, solo | ~$310 | 100% | 3.10 |
| CheapClaude Max 20x | ~$45 | ~20% | 2.25 |
| Anthropic Max 20x direct | $200 | 100% | 2.00 |
| G2G shared Max | ~$35 | ~20% | 1.75 |

Our tickets match Anthropic's retail Pro / Max 5x prices, so they are the dearest option listed.

## Proposed prices (not applied)

Break-even is $2.50 per 1% ($200 ÷ 80% `max_sold_pct`); the login resellers cannot be undercut
without a loss. Day ≈ 15% and week ≈ 35% of the month price.

| Option | $ / 1% | Lite day / week / month | Standard day / week / month | Revenue at 80% sold |
|---|---|---|---|---|
| A. Thin margin (recommended) | 2.75 | $2 / $5 / $14 | $9 / $24 / $69 | $220 (+$20) |
| B. Break-even | 2.50 | $2 / $5 / $12.50 | $8 / $22 / $62.50 | $200 (±0) |
| C. Match CheapClaude | 2.20 | $1.50 / $4 / $11 | $7 / $19 / $55 | $176 (−$24) |

Option A undercuts Anthropic retail by ~30% and the relays. Differentiators against the login
resellers: a guaranteed share, a usage dashboard, bank-transfer payment.

All resale of Pro/Max access, ours included, breaks Anthropic's terms (see the README).
