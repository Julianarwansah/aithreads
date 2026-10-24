export async function loginThreads(browser) {
  const context = await browser.newContext();
  const page = await context.newPage();
  
  try {
    console.log('Navigating to Threads login page...');
    await page.goto('https://www.threads.net/login');
    
    console.log('Please log in to Threads manually. Waiting for homepage...');
    
    // Wait for URL to become exactly https://www.threads.net/
    await page.waitForURL(url => url.href === 'https://www.threads.net/', {
      timeout: 5 * 60 * 1000 // 5 minutes
    });
    
    console.log('Login successful! Saving session state...');
    
    // Save storage state to threads_state.json
    await context.storageState({ path: 'threads_state.json' });
    
    console.log('Threads session saved to threads_state.json');
    
  } catch (error) {
    if (error.name === 'TimeoutError') {
      console.error('Timeout: User did not complete login within 5 minutes');
    } else {
      console.error('Error during Threads login:', error.message);
    }
    throw error;
  } finally {
    await context.close();
  }
}