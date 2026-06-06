# External References

## Data Sources

### USAspending.gov
- **Public API**: https://api.usaspending.gov
- **API Documentation**: https://api.usaspending.gov/docs/endpoints
- **GitHub (schema/terminology reference only)**: https://github.com/fedspendingtransparency/usaspending-api

> Note: The official USAspending API backend is open source (Django/Postgres/Elasticsearch),
> but this project consumes the public production API rather than embedding or running
> the full backend.

### SAM.gov Opportunities API
- **Portal**: https://sam.gov
- **API Documentation**: https://open.gsa.gov/api/get-opportunities-public-api/
- **Developer Access (free API key)**: https://sam.gov/content/entity-registration
- **Base URL**: `https://api.sam.gov/opportunities/v2/search`
- **Award Notice type**: `ptype=a`

### Defense.gov Daily Contracts
- **Contracts Index**: https://www.defense.gov/News/Contracts/
- **Format**: Daily press release pages listing DoD contract announcements
- **Notes**: Each daily page lists contract awards by branch. Parsing is best-effort
  because the format is prose and varies by day.

### SEC Company Tickers
- **company_tickers.json**: https://www.sec.gov/files/company_tickers.json
- **Usage**: Maps CIK → ticker + company name for all registered public companies
- **Update frequency**: Daily (we cache for 24h)

## Market Data

### yfinance (Yahoo Finance)
- **GitHub**: https://github.com/ranaroussi/yfinance
- **PyPI**: https://pypi.org/project/yfinance/
- **Data fields used**: marketCap, totalRevenue, averageVolume, currentPrice,
  sector, industry, exchange, longName

## Key API Endpoints Used (USAspending)

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v2/search/spending_by_award/` | Search awards by date range, amount, type |
| `GET /api/v2/awards/<id>/` | Fetch full detail for a known award ID |
| `POST /api/v2/autocomplete/recipient/` | Fuzzy recipient name search |
| `POST /api/v2/autocomplete/naics/` | NAICS code autocomplete |
| `POST /api/v2/autocomplete/psc/` | PSC code autocomplete |

## Legal / Compliance

This tool operates exclusively on publicly available government data:
- All government contract data is public record under FOIA and the Federal Acquisition Regulation
- SAM.gov, USAspending.gov, and Defense.gov are official government websites
- SEC EDGAR data is public record
- Market data (Yahoo Finance) is publicly available

No authentication circumvention, no scraping behind logins, no rate limit bypassing.
