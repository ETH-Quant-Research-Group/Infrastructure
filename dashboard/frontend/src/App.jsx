import { useState } from 'react'
import logo from './assets/QRFLogo.png'
import Performance from './subpage/Performance'
import Orders from './subpage/Orders'
import Network from './subpage/Network'
// import './App.css'
const TABS = ['Home', 'Strategies', 'Assets', 'Network']

function Home() {
  return (
    <div>
      <div className="flex gap-6 h-full ">
        <div className="flex-1 min-w-0 border-2pt-1 border-white">
          <Performance />
        </div>
        <div className="w-72 shrink-0 border-l  border-zinc-900 pl-6  border-2">
          <Orders />
        </div>

      </div>

    </div>

  )
}

function Strategies() {
  return <div><h2 className="text-xl font-medium">Strategies</h2></div>
}

function Assets() {
  return <div><h2 className="text-xl font-medium">Assets</h2></div>
}

const PAGES = { Home, Strategies, Assets, Network }

function App() {
  const [activeTab, setActiveTab] = useState('Home')
  const Page = PAGES[activeTab]

  return (
    <div className='flex flex-col min-h-screen'>
      <header className="flex items-center gap-8 px-8 h-14 bg-black border-b border-zinc-900">
        <img src={logo} alt="logo" className="h-8 w-auto object-contain" />
        <nav className="flex gap-1">
          {TABS.map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-3.5 py-1.5 rounded-md text-[0.85rem] cursor-pointer transition-colors duration-150 border-0 ${activeTab === tab
                ? 'bg-zinc-800 text-white'
                : 'bg-transparent text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300'
                }`}
            >
              {tab}
            </button>
          ))}
        </nav>
      </header>
      <main className="p-8 flex-1">
        <Page />
      </main>
    </div>
  )
}

export default App
