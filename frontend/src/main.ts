import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import './icons.css'
import { initHljsTheme } from './utils/hljsTheme'

// highlight.js token colors follow the UI theme (file manager code view).
initHljsTheme()

const app = createApp(App)
app.mount('#app')
