# Google Cloud Basics

## Organising resources
Google Cloud resources sit in a hierarchy: an organisation at the top, optional
folders below it, then projects, then the resources themselves. A project is the
main unit of work: it holds resources, has its own enabled APIs and is linked to a
billing account. Policies set higher up are inherited by everything below.

## Regions and zones
A region is an independent geographic area, such as asia-south1 in Mumbai. A zone is
a separate deployment area inside a region. Running across several zones protects a
service from the failure of one zone. Choosing a region affects latency, and it can
also matter for rules about where data is stored.

## Billing and budgets
A billing account pays for the projects linked to it. A budget compares spending with
a planned amount and sends alerts at thresholds. A budget alert only warns: it does
not stop resources or cap spending by itself.

## Identity and Access Management (IAM)
IAM answers the question "who can do what on which resource". A principal is an
identity, such as a user, a group or a service account. A role is a named set of
permissions. An allow policy binds principals to roles on a resource. The principle
of least privilege means granting only the access a principal needs to do its job.

## Service accounts
A service account is an identity for a workload rather than a person. An application
running on Cloud Run can run as a service account and receive short-lived credentials
automatically, so no key file has to be stored with the code. Long-lived service
account keys are a risk because they can leak, so avoid them where possible.

## Cloud Run
Cloud Run runs containers without the user managing servers. It starts instances when
requests arrive and can scale down to zero when there is no traffic. A container
image packages an application with what it needs to run. Each deployment creates a
new revision, which is an unchangeable snapshot of the code and settings, and traffic
can be moved between revisions.

## Building and storing containers
Cloud Build runs build steps in the cloud, including building a container image.
Artifact Registry stores container images and other build outputs. A common path is
that Cloud Build builds an image, stores it in Artifact Registry, and Cloud Run
deploys it.

## Storing data
Cloud Storage holds objects, such as files, in buckets. Firestore is a serverless
document database, suited to application data. BigQuery is a serverless data
warehouse for running analytical SQL queries across large datasets.

## Secrets
Secret Manager stores sensitive values such as API keys and passwords. It keeps them
out of source code, controls who can read them through IAM, and keeps versions of
each secret.

## Generative AI on Google Cloud
Gemini is Google's family of generative AI models. Applications on Google Cloud can
call Gemini through a Google Cloud project, using the identity of a service account
rather than a pasted API key. Requesting a response schema asks the model for
structured output in a fixed shape, which is easier for code to check than free text.
Model output should still be validated, because a well-formed answer can still be
wrong.
