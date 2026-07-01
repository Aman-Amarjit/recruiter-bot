import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.39.7"

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? ""
const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_KEY") ?? ""
const GEMINI_API_KEY = Deno.env.get("GEMINI_API_KEY") ?? ""

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY)

async function analyzeSentiment(text: string): Promise<string> {
  if (!GEMINI_API_KEY) return "neutral"
  try {
    const url = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=${GEMINI_API_KEY}`
    const payload = {
      contents: [{
        parts: [{
          text: `Analyze the sentiment of this recruiter's reply to a cold application. Categorize it strictly as one of: positive, negative, or neutral. Return only the single word. \n\nReply:\n${text}`
        }]
      }]
    }
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
    if (response.ok) {
      const data = await response.json()
      const answer = data.candidates?.[0]?.content?.parts?.[0]?.text?.trim()?.toLowerCase() ?? "neutral"
      if (["positive", "negative", "neutral"].includes(answer)) {
        return answer
      }
    }
    return "neutral"
  } catch (e) {
    console.error("Sentiment analysis failed:", e)
    return "neutral"
  }
}

serve(async (req: Request) => {
  const url = new URL(req.url)
  const method = req.method

  // 1. GET Request: Opt-Out Unsubscribe confirmation page
  if (method === "GET") {
    const email = url.searchParams.get("email")
    if (!email) {
      return new Response("Missing email parameter.", { status: 400 })
    }

    try {
      // Set suppressed = true in Supabase contacts
      const { error } = await supabase
        .from("contacts")
        .update({ suppressed: true })
        .eq("email", email)

      if (error) throw error

      // Return a clean, styled HTML opt-out landing page
      const html = `
      <!DOCTYPE html>
      <html>
      <head>
        <meta charset="utf-8">
        <title>Unsubscribed</title>
        <style>
          body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background-color: #f7f7f7; color: #333; }
          .card { background: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); display: inline-block; max-width: 500px; }
          h2 { color: #d9534f; margin-top: 0; }
          p { line-height: 1.5; color: #555; }
        </style>
      </head>
      <body>
        <div class="card">
          <h2>Opt-Out Complete</h2>
          <p>The email address <strong>${email}</strong> has been successfully removed from Aman Amarjit's freelancer outreach list.</p>
          <p>You will not receive any further automated internship or freelance proposal emails.</p>
        </div>
      </body>
      </html>
      `
      return new Response(html, {
        headers: { "Content-Type": "text/html" }
      })
    } catch (err) {
      console.error("Opt-out error:", err)
      return new Response("Error updating unsubscribe database records.", { status: 500 })
    }
  }

  // 2. POST Request: Resend Webhooks (Bounces & Replies)
  if (method === "POST") {
    try {
      const body = await req.json()
      const eventType = body.type // e.g. "email.bounced" or "email.inbound"
      const data = body.data

      console.log(`Processing Webhook event: ${eventType}`)

      // A. Bounce Webhook
      if (eventType === "email.bounced") {
        const recipient = data.to?.[0]
        if (recipient) {
          // Suppress contact
          await supabase.from("contacts").update({ suppressed: true }).eq("email", recipient)
          
          // Log bounce in send logs
          const { data: contactsData } = await supabase.from("contacts").select("id").eq("email", recipient)
          if (contactsData && contactsData.length > 0) {
            const contactId = contactsData[0].id
            const { data: appsData } = await supabase.from("applications").select("id").eq("contact_id", contactId).order("created_at", { ascending: false }).limit(1)
            if (appsData && appsData.length > 0) {
              const appId = appsData[0].id
              await supabase.from("send_log").update({ bounced: true }).eq("application_id", appId)
            }
          }
          console.log(`Bounce logged & contact suppressed: ${recipient}`)
        }
      }

      // B. Inbound Reply Webhook
      // Resend posts inbound replies with target details
      if (eventType === "email.inbound" || (data && data.from && data.text)) {
        const fromHeader = data.from ?? ""
        const textBody = data.text ?? ""
        
        // Extract raw email from "Sender Name <sender@example.com>"
        const emailMatch = fromHeader.match(/<([^>]+)>/) ?? fromHeader.match(/([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/)
        const recruiterEmail = emailMatch ? emailMatch[1].trim().toLowerCase() : ""

        if (recruiterEmail) {
          // Find contact
          const { data: contactsData } = await supabase.from("contacts").select("id").eq("email", recruiterEmail)
          if (contactsData && contactsData.length > 0) {
            const contactId = contactsData[0].id
            
            // Find application
            const { data: appsData } = await supabase
              .from("applications")
              .select("id")
              .eq("contact_id", contactId)
              .order("created_at", { ascending: false })
              .limit(1)
              
            if (appsData && appsData.length > 0) {
              const appId = appsData[0].id
              
              // Run sentiment check
              const sentiment = await analyzeSentiment(textBody)
              
              // Update application status to replied
              await supabase.from("applications").update({
                status: "replied",
                reply_sentiment: sentiment
              }).eq("id", appId)
              
              console.log(`Reply tracked for ${recruiterEmail}. Sentiment: ${sentiment}`)
            }
          }
        }
      }

      return new Response(JSON.stringify({ success: true }), {
        headers: { "Content-Type": "application/json" }
      })
    } catch (e) {
      console.error("Webhook processing error:", e)
      return new Response(JSON.stringify({ error: e.message }), { status: 500 })
    }
  }

  return new Response("Method not supported.", { status: 405 })
})
