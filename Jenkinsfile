pipeline {
  agent any

  environment {
    AWS_REGION   = 'eu-north-1'
    AWS_ACCOUNT  = '283904064984'
    ECR_REGISTRY = "${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
    CLUSTER_NAME = 'shivam-hospital-prod'
    REPO_URL     = '://github.com'
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }

    stage('SonarQube Analysis') {
      steps {
        script {
          def scannerHome = tool 'sonar-scanner'
          withSonarQubeEnv('sonarqube') {
            withCredentials([string(credentialsId: 'sonar', variable: 'SONAR_TOKEN')]) {
              sh """
                ${scannerHome}/bin/sonar-scanner \
                  -Dsonar.projectKey=shivam-hospital \
                  -Dsonar.sources=services \
                  -Dsonar.exclusions=**/node_modules/**,**/.git/**,**/*.log,**/venv/** \
                  -Dsonar.login=$SONAR_TOKEN
              """
            }
          }
        }
      }
    }

    stage('Build Images') {
      steps {
        script {
          def services = ['auth','appointment','records','billing','notification','inventory']
          services.each { svc ->
            sh "docker build -t ${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER} -f services/${svc}/Dockerfile services/"
          }
        }
      }
    } 

    stage('Trivy Scan') {
      steps {
        script {
          def services = ['auth','appointment','records','billing','notification','inventory']
          sh "trivy clean --scan-cache"
          services.each { svc ->
            sh "trivy image --cache-dir .trivycache-${svc} --severity HIGH,CRITICAL --exit-code 0 --no-progress ${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER}"
          }
        }
      }
    }

    stage('Push to ECR') {
      steps {
        withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-creds']]) {
          sh "aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY"
          script {
            def services = ['auth','appointment','records','billing','notification','inventory']
            services.each { svc ->
              sh "docker push ${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER}"
            }
          }
        }
      }
    }

    // 🐙 GITOPS DEPLOY (This replaces the old EKS stage)
    stage('Update Manifests in GitHub') {
      steps {
        script {
          sh """
            git config user.email "jenkins-bot@hospital.com"
            git config user.name "Jenkins Bot"
          """

          withCredentials([string(credentialsId: 'github-token', variable: 'GITHUB_TOKEN')]) {
            def services = ['auth','appointment','records','billing','notification','inventory']
            services.each { svc ->
              echo "✏️ Updating image for ${svc}..."
              // Updates the YAML files in kubernetes/deployments/
              sh "sed -i 's|image: .*/${svc}-service:.*|image: ${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER}|g' kubernetes/deployments/${svc}.yaml"
            }

            sh """
              git add kubernetes/deployments/*.yaml
              git commit -m "chore: update images to build ${BUILD_NUMBER} [skip ci]"
              git push https://${GITHUB_TOKEN}@${REPO_URL} HEAD:main
            """
          }
        }
      }
    }
  }

  post {
    success { echo '✅ Deployment manifests updated! ArgoCD will sync now.' }
    failure { echo '❌ Pipeline failed — check logs' }
    always {
        sh "docker system prune -f" // Cleans up space
    }
  }
}
