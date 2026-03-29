pipeline {
  agent any

  environment {
    AWS_REGION   = 'eu-north-1'
    AWS_ACCOUNT  = '283904064984'
    ECR_REGISTRY = "${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
    CLUSTER_NAME = 'shivam-hospital-prod'
  }

  stages {

    stage('Checkout') {
      steps {
        checkout scm
      }
    }

    // 🔍 SONARQUBE ANALYSIS
    stage('SonarQube Analysis') {
      steps {
        withSonarQubeEnv('sonarqube') {
          sh '''
            sonar-scanner \
              -Dsonar.projectKey=shivam-hospital \
              -Dsonar.sources=. \
              -Dsonar.host.url=http://13.60.99.233:9000 \
              -Dsonar.login=$SONAR_AUTH_TOKEN\
          '''
        }
      }
    }

    // 🚫 QUALITY GATE
    stage('Quality Gate') {
      steps {
        timeout(time: 2, unit: 'MINUTES') {
          waitForQualityGate abortPipeline: true
        }
      }
    }

    // 🐳 BUILD
    stage('Build Images') {
      steps {
        script {
          def services = ['auth','appointment','records','billing','notification','inventory']
          services.each { svc ->
            sh "docker build -t ${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER} ./services/${svc}"
          }
        }
      }
    }

    // 🔐 TRIVY SCAN
    stage('Trivy Scan') {
      steps {
        script {
          def services = ['auth','appointment','records','billing','notification','inventory']
          services.each { svc ->
            sh """
              trivy image \
              --severity HIGH,CRITICAL \
              --exit-code 1 \
              --no-progress \
              ${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER}
            """
          }
        }
      }
    }

    // 📦 PUSH TO ECR
    stage('Push to ECR') {
      steps {
        withCredentials([[
          $class: 'AmazonWebServicesCredentialsBinding',
          credentialsId: 'aws-creds'
        ]]) {
          sh 'aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY'

          script {
            def services = ['auth','appointment','records','billing','notification','inventory']
            services.each { svc ->
              sh "docker push ${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER}"
            }
          }
        }
      }
    }

    // 🚀 DEPLOY
    stage('Deploy to EKS') {
      steps {
        withCredentials([[
          $class: 'AmazonWebServicesCredentialsBinding',
          credentialsId: 'aws-creds'
        ]]) {
          sh 'aws eks update-kubeconfig --name $CLUSTER_NAME --region $AWS_REGION'

          script {
            def services = ['auth','appointment','records','billing','notification','inventory']
            services.each { svc ->
              sh "kubectl set image deployment/${svc}-service ${svc}=${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER} -n backend"
            }
          }
        }
      }
    }
  }

  post {
    success { echo '✅ Deployment successful!' }
    failure { echo '❌ Pipeline failed — check logs' }
  }
}
