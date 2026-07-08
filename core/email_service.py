import yagmail


class EmailService:

    def __init__(self, user, password):
        self.yag = yagmail.SMTP(user=user, password=password)

    def send_report(self, to, subject, body):

        self.yag.send(
            to=to,
            subject=subject,
            contents=body
        )