import { chromium } from 'playwright-extra';
import stealthPlugin from 'puppeteer-extra-plugin-stealth';

export async function launchStealthBrowser() {
  chromium.use(stealthPlugin());
  
  const browser = await chromium.launch({
    headless: false
  });
  
  return browser;
}