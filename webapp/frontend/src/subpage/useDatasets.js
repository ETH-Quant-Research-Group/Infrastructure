import { useEffect, useState } from 'react'

// The history database's datasets (webapp/api/backtest.py GET /datasets),
// loaded once; [] until they arrive or if the request fails.
export function useDatasets() {
  const [datasets, setDatasets] = useState([])
  useEffect(() => {
    fetch('/api/backtest/datasets')
      .then(res => (res.ok ? res.json() : { datasets: [] }))
      .then(d => setDatasets(d.datasets ?? []))
      .catch(() => {})
  }, [])
  return datasets
}
